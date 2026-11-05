"""Grants below connection level: a role on one database, schema or table (collection).

A scoped grant *adds* the role's permissions for objects inside its scope and nothing anywhere
else (deny by default). Only object-level permissions are scopable; anything that cannot be
confined to an object stays a connection-level right: free-form SQL / aggregation, database
creation and removal, access management, governance.

Targets are described by :class:`ObjectScope`. ``inside=True`` is used for *listing* a
container (databases of a server, tables of a schema): any grant inside the container makes it
visible, and the listing is then filtered to what the grants cover. Without ``inside``, a grant
must be at least as broad as the target (a table grant never authorises a schema-wide action).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.auth.permissions import Perm

SCOPABLE: frozenset[str] = frozenset(
    p.value
    for p in (
        Perm.METADATA_VIEW,
        Perm.DATA_READ,
        Perm.DATA_READ_SENSITIVE,
        Perm.DATA_INSERT,
        Perm.DATA_UPDATE,
        Perm.DATA_DELETE,
        Perm.DATA_BULK_WRITE,
        Perm.EXPORT_EXPORT,
        Perm.EXPORT_IMPORT,
        Perm.ADMIN_MANAGE_SCHEMAS,
        Perm.ADMIN_MANAGE_INDEXES,
    )
)


@dataclass(frozen=True)
class ObjectScope:
    database: str
    schema: str | None = None
    object: str | None = None
    inside: bool = False  # listing a container: narrower grants inside it count


@dataclass(frozen=True)
class ScopedGrant:
    database: str
    schema: str  # "" = every schema of the database
    object: str  # "" = every object of the schema / database
    permissions: frozenset[str]


def covers(g: ScopedGrant, t: ObjectScope) -> bool:
    if g.database != t.database:
        return False
    schema = t.schema or ""
    if t.inside:
        # container listing: the grant only has to touch the container
        if t.schema is None:
            return True
        if g.schema not in ("", schema):
            return False
        return t.object is None or g.object in ("", t.object)
    if t.object is not None:
        return g.schema in ("", schema) and g.object in ("", t.object)
    if t.schema is not None:  # a schema as a whole
        return g.schema in ("", schema) and g.object == ""
    return g.schema == "" and g.object == ""  # a database as a whole


def scoped_permissions(grants: Sequence[ScopedGrant], t: ObjectScope) -> frozenset[str]:
    out: set[str] = set()
    for g in grants:
        if covers(g, t):
            out |= g.permissions
    return frozenset(out)


def filter_listing[T](  # noqa: PLR0913
    grants: Sequence[ScopedGrant],
    items: list[T],
    name: Callable[[T], str],
    level: str,
    *,
    database: str | None = None,
    schema: str | None = None,
) -> list[T]:
    """Keep what the (metadata-viewing) grants make visible.

    ``level`` is what is being listed: ``databases``, ``schemas`` (of ``database``) or
    ``tables`` (tables / collections of ``database`` and, where the engine has one, ``schema``).
    """
    viewing = [g for g in grants if Perm.METADATA_VIEW.value in g.permissions]

    def visible(n: str) -> bool:
        for g in viewing:
            if level == "databases":
                ok = g.database == n
            elif level == "schemas":
                ok = g.database == database and g.schema in ("", n)
            else:
                ok = (
                    g.database == database
                    and g.schema in ("", schema or "")
                    and g.object in ("", n)
                )
            if ok:
                return True
        return False

    return [it for it in items if visible(name(it))]
