"""MongoDB adapter (PyMongo). Read-only operations in this phase.

Topology discovery is disabled (``directConnection=True``): PyMongo would otherwise follow
member addresses advertised by the server, which would bypass the outbound network policy.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pymongo.errors
from bson import json_util
from pymongo import MongoClient

from app.database.core.base import Capabilities, TestResult, TlsMode
from app.database.core.serialization import approx_size
from app.database.document.base import DocumentAdapter
from app.database.document.filters import (
    decode_id_cursor,
    decode_offset_cursor,
    encode_id_cursor,
    encode_offset_cursor,
)
from app.database.document.models import (
    CollectionDetail,
    CollectionInfo,
    DocDatabaseInfo,
    DocIndexInfo,
    DocPage,
    DocQuery,
)
from app.errors import (
    Conflict,
    ConnectionFailed,
    Forbidden,
    NotEditable,
    NotFound,
    QueryFailed,
    StatementTimeout,
    ValidationFailed,
)
from app.observability import scrub

MAX_DOC_BYTES = 1024 * 1024
_MAX_TIME_MS_CODE = 50
_AUTH_FAILED_CODE = 18


def to_extended_json(doc: Any) -> Any:
    """Canonical Extended JSON as plain Python (preserves BSON types)."""
    return json.loads(json_util.dumps(doc, json_options=json_util.CANONICAL_JSON_OPTIONS))


class MongoAdapter(DocumentAdapter):
    capabilities = Capabilities(
        family="document",
        engine="mongodb",
        supports_indexes=True,
        supports_read_only_transactions=False,
        supports_keyset_pagination=True,
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client_obj: MongoClient[Any] | None = None
        self._lock = threading.Lock()
        self._ca_path: str | None = None
        from app.database.core.base import TtlCache

        self._meta = TtlCache(ttl=30.0)

    # ---- connection handling ----------------------------------------------------------

    def _ca_file(self) -> str | None:
        if not self.spec.tls_ca:
            return None
        if self._ca_path is None:
            fd, path = tempfile.mkstemp(prefix="dbw-ca-", suffix=".pem")
            with os.fdopen(fd, "w") as fh:
                fh.write(self.spec.tls_ca)
            self._ca_path = path
        return self._ca_path

    def _client(self) -> MongoClient[Any]:
        with self._lock:
            if self._client_obj is not None:
                return self._client_obj
            s = self.spec
            pinned = self.policy.resolve(s.host, s.port)[0]
            mode = s.tls_mode
            timeout_ms = self.limits.statement_timeout_s * 1000
            kwargs: dict[str, Any] = {
                "port": s.port,
                "username": s.username or None,
                "password": s.password or None,
                "authSource": s.options.get("auth_source", "admin"),
                "directConnection": True,
                "serverSelectionTimeoutMS": s.connect_timeout_s * 1000,
                "connectTimeoutMS": s.connect_timeout_s * 1000,
                "socketTimeoutMS": timeout_ms + 5000,
                "maxPoolSize": max(1, s.pool_max),
                "appname": "db-wrench",
                "retryWrites": False,
                "tz_aware": True,
            }
            if mode == TlsMode.DISABLE:
                kwargs["host"] = pinned
                kwargs["tls"] = False
            else:
                kwargs["tls"] = True
                ca = self._ca_file()
                if ca:
                    kwargs["tlsCAFile"] = ca
                if mode == TlsMode.REQUIRE:
                    kwargs["host"] = pinned
                    kwargs["tlsAllowInvalidCertificates"] = True
                elif mode == TlsMode.VERIFY_CA:
                    kwargs["host"] = pinned
                    kwargs["tlsAllowInvalidHostnames"] = True
                else:  # verify-full: connect by name so the hostname is verified
                    kwargs["host"] = s.host
            self._client_obj = MongoClient(**kwargs)
            return self._client_obj

    @contextmanager
    def _errors(self) -> Iterator[None]:
        try:
            yield
        except pymongo.errors.ExecutionTimeout as exc:
            raise StatementTimeout() from exc
        except pymongo.errors.OperationFailure as exc:
            if exc.code == _MAX_TIME_MS_CODE:
                raise StatementTimeout() from exc
            if exc.code == _AUTH_FAILED_CODE:
                raise ConnectionFailed("Authentication failed.") from exc
            msg = (exc.details or {}).get("errmsg") or str(exc)
            raise QueryFailed(scrub(str(msg), self.spec.password)) from exc
        except pymongo.errors.ConfigurationError as exc:
            raise ConnectionFailed(scrub(str(exc), self.spec.password)) from exc
        except pymongo.errors.PyMongoError as exc:  # connection / selection / network timeouts
            raise ConnectionFailed(scrub(str(exc).splitlines()[0], self.spec.password)) from exc

    def close(self) -> None:
        with self._lock:
            client, self._client_obj = self._client_obj, None
        if client:
            client.close()
        if self._ca_path:
            with contextlib.suppress(OSError):
                os.unlink(self._ca_path)
            self._ca_path = None

    def test(self) -> TestResult:
        started = time.perf_counter()
        try:
            with self._errors():
                client = self._client()
                client.admin.command("ping")
                version = client.admin.command("buildInfo").get("version")
        except Exception as exc:
            code = getattr(exc, "code", "connection_failed")
            return TestResult(False, error=getattr(exc, "detail", str(exc)), error_code=code)
        return TestResult(True, str(version), round((time.perf_counter() - started) * 1000, 1))

    # ---- metadata ---------------------------------------------------------------------

    def list_databases(self) -> list[DocDatabaseInfo]:
        def load() -> list[DocDatabaseInfo]:
            with self._errors():
                try:
                    docs = list(self._client().list_databases())
                except pymongo.errors.OperationFailure as exc:
                    # Accounts without listDatabases may still use their configured database.
                    if exc.code in (13, 8000) and self.spec.database:
                        return [DocDatabaseInfo(name=self.spec.database)]
                    raise
            return [DocDatabaseInfo(d["name"], d.get("sizeOnDisk"), d.get("empty")) for d in docs]

        return self._meta.get_or_load(("databases",), load)

    def _require_database(self, database: str) -> None:
        if database not in {d.name for d in self.list_databases()}:
            raise NotFound(f"Database {database!r} was not found.")

    def list_collections(self, database: str) -> list[CollectionInfo]:
        self._require_database(database)

        def load() -> list[CollectionInfo]:
            with self._errors():
                items = list(self._client()[database].list_collections())
            return sorted(
                (
                    CollectionInfo(c["name"], c.get("type", "collection"))
                    for c in items
                    if not c["name"].startswith("system.")
                ),
                key=lambda c: c.name,
            )

        return self._meta.get_or_load(("collections", database), load)

    def _require_collection(self, database: str, collection: str) -> CollectionInfo:
        for c in self.list_collections(database):
            if c.name == collection:
                return c
        raise NotFound(f"Collection {database}.{collection} was not found.")

    def get_collection(self, database: str, collection: str) -> CollectionDetail:
        info = self._require_collection(database, collection)
        timeout = self.limits.statement_timeout_s * 1000
        coll = self._client()[database][collection]
        with self._errors():
            options = (
                self._client()[database].list_collections(filter={"name": collection}).try_next()
                or {}
            ).get("options", {})
            stats: dict[str, Any] = {}
            if info.type == "collection":
                rows = list(
                    coll.aggregate(
                        [{"$collStats": {"storageStats": {}, "count": {}}}], maxTimeMS=timeout
                    )
                )
                if rows:
                    st = rows[0].get("storageStats", {})
                    stats = {
                        "count": rows[0].get("count", st.get("count")),
                        "size": st.get("size"),
                        "avg_obj_size": st.get("avgObjSize"),
                        "storage_size": st.get("storageSize"),
                        "total_index_size": st.get("totalIndexSize"),
                        "index_count": st.get("nindexes"),
                        "capped": st.get("capped", False),
                    }
            indexes = (
                [
                    DocIndexInfo(
                        name=ix["name"],
                        keys=[[k, v] for k, v in ix["key"].items()],
                        unique=bool(ix.get("unique")),
                        sparse=bool(ix.get("sparse")),
                        ttl_seconds=ix.get("expireAfterSeconds"),
                        partial_filter=to_extended_json(ix["partialFilterExpression"])
                        if "partialFilterExpression" in ix
                        else None,
                    )
                    for ix in coll.list_indexes()
                ]
                if info.type == "collection"
                else []
            )
        return CollectionDetail(
            name=collection,
            type=info.type,
            options=to_extended_json(options),
            capped=bool(stats.get("capped") or options.get("capped")),
            stats=stats,
            indexes=indexes,
        )

    # ---- documents --------------------------------------------------------------------

    def find_documents(
        self,
        database: str,
        collection: str,
        q: DocQuery,
        *,
        timeout_s: int | None = None,
        comment: str | None = None,
    ) -> DocPage:
        self._require_collection(database, collection)
        coll = self._client()[database][collection]
        limit = max(1, min(q.limit, self.limits.max_page_size, self.limits.max_result_rows))
        timeout = (timeout_s or self.limits.statement_timeout_s) * 1000
        warnings: list[str] = []
        if limit < q.limit:
            warnings.append(f"Page size was limited to {limit} documents.")

        flt: dict[str, Any] = q.filter
        skip = 0
        keyset = not q.sort
        sort = q.sort or [("_id", 1)]
        if keyset and q.cursor:
            flt = (
                {"$and": [q.filter, {"_id": {"$gt": decode_id_cursor(q.cursor)}}]}
                if q.filter
                else {"_id": {"$gt": decode_id_cursor(q.cursor)}}
            )
        elif not keyset:
            if q.sort and not any(f == "_id" for f, _ in q.sort):
                sort = [*q.sort, ("_id", 1)]  # deterministic tie-breaker
            if q.cursor:
                skip = decode_offset_cursor(q.cursor, self.limits.max_offset)

        with self._errors():
            cur = coll.find(
                flt,
                q.projection,
                sort=sort,
                skip=skip,
                limit=limit + 1,
                max_time_ms=timeout,
                comment=comment,
            )
            raw = list(cur)
            estimate = None if q.filter else coll.estimated_document_count(maxTimeMS=timeout)

        has_more = len(raw) > limit
        raw = raw[:limit]
        docs: list[dict[str, Any]] = []
        doc_keys: list[str | None] = []
        size = 0
        clipped = False
        last_included = 0
        for i, d in enumerate(raw):
            ej = to_extended_json(d)
            doc_size = approx_size(ej)
            if doc_size > MAX_DOC_BYTES:
                ej = {
                    "_id": to_extended_json(d.get("_id")),
                    "$truncated": True,
                    "approx_size_bytes": doc_size,
                }
                doc_size = 64
            size += doc_size
            if docs and size > self.limits.max_response_bytes:
                clipped = has_more = True
                break
            docs.append(ej)
            doc_keys.append(encode_id_cursor(d["_id"]) if "_id" in d else None)
            last_included = i

        next_cursor: str | None = None
        if has_more and docs:
            if keyset:
                last_id = raw[last_included].get("_id")
                if last_id is not None:
                    next_cursor = encode_id_cursor(last_id)
            else:
                next_cursor = encode_offset_cursor(skip + len(docs))
        if clipped:
            warnings.append("Response was cut at the size limit.")
        return DocPage(
            documents=docs,
            limit=limit,
            next_cursor=next_cursor,
            total_estimate=estimate,
            truncated=limit < q.limit or clipped,
            pagination="keyset" if keyset else "offset",
            warnings=warnings,
            doc_keys=doc_keys,
        )

    # ---- Phase 3: writes and streaming ------------------------------------------------

    def _writable_collection(self, database: str, collection: str) -> Any:
        if not self.spec.allow_writes:
            raise Forbidden("This connection is read-only.")
        info = self._require_collection(database, collection)
        if info.type != "collection":
            raise NotEditable("Views and time-series collections cannot be edited here.")
        return self._client()[database][collection]

    @staticmethod
    def _cas_filter(doc_id: Any, original: dict[str, Any] | None) -> dict[str, Any]:
        """Match the document only while it still equals what the user saw (compare-and-swap)."""
        flt: dict[str, Any] = {"_id": doc_id}
        if original:
            flt.update({k: v for k, v in original.items() if k != "_id"})
        return flt

    def insert_document(
        self, database: str, collection: str, doc: dict[str, Any]
    ) -> dict[str, Any]:
        coll = self._writable_collection(database, collection)
        with self._errors():
            try:
                res = coll.insert_one(doc)
            except pymongo.errors.DuplicateKeyError as exc:
                raise Conflict("A document with this _id or a unique key already exists.") from exc
            stored = coll.find_one({"_id": res.inserted_id})
        return to_extended_json(stored)

    def replace_document(
        self,
        database: str,
        collection: str,
        doc_key: str,
        original: dict[str, Any],
        new: dict[str, Any],
    ) -> dict[str, Any]:
        coll = self._writable_collection(database, collection)
        doc_id = decode_id_cursor(doc_key)
        if "_id" in new and new["_id"] != doc_id:
            raise ValidationFailed("_id cannot be changed.")
        if "_id" in original and original["_id"] != doc_id:
            raise ValidationFailed("The original document does not match this _id.")
        new = {**new, "_id": doc_id}
        with self._errors():
            try:
                res = coll.replace_one(self._cas_filter(doc_id, original), new)
            except pymongo.errors.DuplicateKeyError as exc:
                raise Conflict("The change violates a unique index.") from exc
            if res.matched_count != 1:
                raise Conflict(
                    "The document was changed or deleted by someone else. Reload it and try again."
                )
            stored = coll.find_one({"_id": doc_id})
        return to_extended_json(stored)

    def delete_document(
        self, database: str, collection: str, doc_key: str, original: dict[str, Any] | None
    ) -> None:
        coll = self._writable_collection(database, collection)
        doc_id = decode_id_cursor(doc_key)
        with self._errors():
            res = coll.delete_one(self._cas_filter(doc_id, original))
        if res.deleted_count != 1:
            raise Conflict(
                "The document was changed or deleted by someone else. Reload it and try again."
            )

    def _matching_ids(self, coll: Any, flt: dict[str, Any], cap: int) -> list[Any]:
        timeout = self.limits.statement_timeout_s * 1000
        with self._errors():
            ids = [d["_id"] for d in coll.find(flt, {"_id": 1}, limit=cap + 1, max_time_ms=timeout)]
        if len(ids) > cap:
            raise ValidationFailed(
                f"This would affect more than {cap} documents. Narrow the filter and retry."
            )
        return ids

    def bulk_preview(
        self, database: str, collection: str, flt: dict[str, Any]
    ) -> tuple[int, list[dict[str, Any]]]:
        self._require_collection(database, collection)
        coll = self._client()[database][collection]
        ids = self._matching_ids(coll, flt, self.limits.bulk_max_rows)
        timeout = self.limits.statement_timeout_s * 1000
        with self._errors():
            sample = list(coll.find(flt, limit=10, max_time_ms=timeout))
        return len(ids), [to_extended_json(d) for d in sample]

    def bulk_delete(
        self, database: str, collection: str, flt: dict[str, Any], expected_count: int
    ) -> int:
        coll = self._writable_collection(database, collection)
        ids = self._matching_ids(coll, flt, self.limits.bulk_max_rows)
        if len(ids) != expected_count:
            raise Conflict(
                f"The selection changed since the preview ({len(ids)} documents now, "
                f"{expected_count} previewed). Nothing was modified; preview again."
            )
        # Pin the operation to the previewed _ids so documents added meanwhile are untouched.
        with self._errors():
            res = coll.delete_many({"$and": [{"_id": {"$in": ids}}, flt]})
        return int(res.deleted_count)

    def bulk_set(
        self,
        database: str,
        collection: str,
        flt: dict[str, Any],
        values: dict[str, Any],
        expected_count: int,
    ) -> int:
        coll = self._writable_collection(database, collection)
        ids = self._matching_ids(coll, flt, self.limits.bulk_max_rows)
        if len(ids) != expected_count:
            raise Conflict(
                f"The selection changed since the preview ({len(ids)} documents now, "
                f"{expected_count} previewed). Nothing was modified; preview again."
            )
        with self._errors():
            res = coll.update_many({"$and": [{"_id": {"$in": ids}}, flt]}, {"$set": values})
        return int(res.matched_count)

    def export_documents(
        self,
        database: str,
        collection: str,
        flt: dict[str, Any],
        sort: list[tuple[str, int]],
        projection: dict[str, int] | None,
    ) -> Iterator[list[dict[str, Any]]]:
        self._require_collection(database, collection)
        coll = self._client()[database][collection]
        timeout = self.limits.statement_timeout_s * 1000
        cap = self.limits.export_max_rows
        with self._errors():
            if coll.count_documents(flt, limit=cap + 1, maxTimeMS=timeout) > cap:
                raise ValidationFailed(
                    f"The export would exceed {cap:,} rows. Add filters to narrow it down."
                )
        return self._export_iter(coll, flt, sort or [("_id", 1)], projection)

    def _export_iter(
        self,
        coll: Any,
        flt: dict[str, Any],
        sort: list[tuple[str, int]],
        projection: dict[str, int] | None,
    ) -> Iterator[list[dict[str, Any]]]:
        with self._errors():
            cur = coll.find(flt, projection, sort=sort, batch_size=500)
            batch: list[dict[str, Any]] = []
            try:
                for d in cur:
                    batch.append(to_extended_json(d))
                    if len(batch) >= 500:
                        yield batch
                        batch = []
                if batch:
                    yield batch
            finally:
                cur.close()

    # ---- Phase 4: workspace -------------------------------------------------------------

    def run_find(
        self, database: str, collection: str, q: DocQuery, *, timeout_s: int, comment: str
    ) -> DocPage:
        return self.find_documents(database, collection, q, timeout_s=timeout_s, comment=comment)

    def run_aggregate(
        self,
        database: str,
        collection: str,
        pipeline: list[dict[str, Any]],
        *,
        max_docs: int,
        timeout_s: int,
        comment: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        info = self._require_collection(database, collection)
        coll = self._client()[database][collection]
        with self._errors():
            cur = coll.aggregate(
                pipeline,
                maxTimeMS=timeout_s * 1000,
                allowDiskUse=False,
                comment=comment,
                batchSize=min(max_docs + 1, 1000),
            )
            try:
                raw = [d for _, d in zip(range(max_docs + 1), cur, strict=False)]
            finally:
                cur.close()
        del info
        truncated = len(raw) > max_docs
        return [to_extended_json(d) for d in raw[:max_docs]], truncated

    def cancel_operation(self, comment: str) -> bool:
        with self._errors():
            client = self._client()
            ops = client.admin.command({"currentOp": 1, "active": True}).get("inprog", [])
            killed = False
            for op in ops:
                cmd = op.get("command") or {}
                if cmd.get("comment") == comment and "opid" in op:
                    client.admin.command({"killOp": 1, "op": op["opid"]})
                    killed = True
            return killed
