"""Database users, roles and privileges (Phase 6).

Connection-level only (never reachable through scoped grants). Every write: permission ->
typed confirmation -> approval policy -> dedicated writable connection -> audit. Passwords are
write-only: they are never returned, stored, audited or placed in an approval summary.
"""

# NOTE: no `from __future__ import annotations` (same reason as api/administration.py).
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, SecretStr

from app.api.administration import _execute
from app.api.schemas import ApprovalId, Strict
from app.auth.deps import AppSettings, CurrentAuth, DbSession
from app.auth.permissions import Perm
from app.connections import service as conns
from app.database.relational.principals import GrantTarget
from app.editor.guards import require_confirmation, require_typed_name
from app.services.runtime import run_blocking

router = APIRouter(prefix="/connections/{cid}", tags=["principals"])
mongo = APIRouter(prefix="/connections/{cid}/mongo/databases/{database}", tags=["principals"])


def _host_param(
    host: Annotated[str | None, Query(max_length=255, description="MySQL account host")] = None,
) -> str | None:
    return host


Host = Annotated[str | None, Depends(_host_param)]


# ---------------------------------------------------------------------------- schemas


class PrincipalOut(BaseModel):
    name: str
    kind: str
    can_login: bool
    protected: bool
    host: str | None
    member_of: list[str]
    attributes: dict[str, Any]


class PrincipalCreate(Strict):
    name: str = Field(min_length=1, max_length=64)
    password: SecretStr | None = None
    host: str | None = Field(default=None, max_length=255)
    options: dict[str, Any] = Field(default_factory=dict)


class PrincipalUpdate(Strict):
    password: SecretStr | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class DropBody(Strict):
    confirm: str | None = Field(default=None, max_length=255)


class TargetIn(Strict):
    kind: Literal["database", "schema", "table", "all_tables"]
    database: str | None = Field(default=None, max_length=255)
    schema_name: str | None = Field(default=None, alias="schema", max_length=255)
    object: str | None = Field(default=None, max_length=255)

    model_config = {"extra": "forbid", "populate_by_name": True}


class PrivilegeChange(Strict):
    privileges: list[str] = Field(min_length=1, max_length=40)
    target: TargetIn
    confirm: str | None = Field(default=None, max_length=255)


class MembershipBody(Strict):
    role: str = Field(min_length=1, max_length=64)
    confirm: str | None = Field(default=None, max_length=255)


class GrantOut(BaseModel):
    kind: str
    database: str | None
    schema_name: str | None = Field(alias="schema")
    object: str | None
    privileges: list[str]
    grantable: bool

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------- relational


async def _open(
    db: DbSession, ctx: CurrentAuth, settings: AppSettings, cid: uuid.UUID, perm: Perm
) -> Any:
    access, adapter = await conns.open_adapter(db, ctx, settings, cid, perm, "relational")
    return access, conns.as_relational(adapter)


@router.get("/principals", response_model=list[PrincipalOut])
async def list_principals(
    cid: uuid.UUID, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> list[PrincipalOut]:
    _, rel = await _open(db, ctx, settings, cid, Perm.ADMIN_VIEW_USERS)
    return [PrincipalOut(**p.__dict__) for p in await run_blocking(rel.list_principals)]


@router.post("/principals", status_code=201, response_model=PrincipalOut)
async def create_principal(  # noqa: PLR0913
    cid: uuid.UUID,
    body: PrincipalCreate,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    approval_id: ApprovalId = None,
) -> PrincipalOut:
    access, rel = await _open(db, ctx, settings, cid, Perm.ADMIN_MANAGE_USERS)
    pw = body.password.get_secret_value() if body.password else None
    await _execute(
        ctx, access, db, action="admin.principal.create", approval_op="admin.create_principal",
        target={"principal": body.name, "host": body.host},
        params={"options": body.options, "password": "set" if pw else None},
        approval_id=approval_id,
        work=lambda: run_blocking(
            lambda: rel.create_principal(
                body.name, password=pw, host=body.host, options=body.options
            )
        ),
    )  # fmt: skip
    created = next(p for p in await run_blocking(rel.list_principals) if p.name == body.name)
    return PrincipalOut(**created.__dict__)


@router.patch("/principals/{name}", status_code=204)
async def alter_principal(  # noqa: PLR0913
    cid: uuid.UUID,
    name: str,
    body: PrincipalUpdate,
    host: Host,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    approval_id: ApprovalId = None,
) -> None:
    access, rel = await _open(db, ctx, settings, cid, Perm.ADMIN_MANAGE_USERS)
    pw = body.password.get_secret_value() if body.password else None
    await _execute(
        ctx, access, db, action="admin.principal.alter", approval_op="admin.alter_principal",
        target={"principal": name, "host": host},
        params={"options": body.options, "password": "set" if pw else None},
        approval_id=approval_id,
        work=lambda: run_blocking(
            lambda: rel.alter_principal(name, host=host, password=pw, options=body.options)
        ),
    )  # fmt: skip


@router.delete("/principals/{name}", status_code=204)
async def drop_principal(  # noqa: PLR0913
    cid: uuid.UUID,
    name: str,
    body: DropBody,
    host: Host,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    approval_id: ApprovalId = None,
) -> None:
    access, rel = await _open(db, ctx, settings, cid, Perm.ADMIN_MANAGE_USERS)
    require_typed_name(body.confirm, name)
    await _execute(
        ctx, access, db, action="admin.principal.drop", approval_op="admin.drop_principal",
        target={"principal": name, "host": host}, params={}, approval_id=approval_id,
        work=lambda: run_blocking(rel.drop_principal, name, host=host),
    )  # fmt: skip


@router.get(
    "/principals/{name}/grants", response_model=list[GrantOut], response_model_by_alias=True
)
async def principal_grants(  # noqa: PLR0913
    cid: uuid.UUID,
    name: str,
    host: Host,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    database: Annotated[str | None, Query(max_length=255)] = None,
) -> list[GrantOut]:
    _, rel = await _open(db, ctx, settings, cid, Perm.ADMIN_VIEW_USERS)
    entries = await run_blocking(
        lambda: rel.list_principal_grants(name, host=host, database=database)
    )
    return [
        GrantOut(
            kind=e.kind,
            database=e.database,
            schema_name=e.schema,
            object=e.object,
            privileges=e.privileges,
            grantable=e.grantable,
        )  # fmt: skip
        for e in entries
    ]


async def _change(  # noqa: PLR0913
    grant: bool,
    cid: uuid.UUID,
    name: str,
    body: PrivilegeChange,
    host: str | None,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    approval_id: uuid.UUID | None,
) -> None:
    access, rel = await _open(db, ctx, settings, cid, Perm.ADMIN_MANAGE_PRIVILEGES)
    require_confirmation(access, body.confirm, name)
    t = body.target
    target = GrantTarget(t.kind, t.database, t.schema_name, t.object)
    verb = "grant" if grant else "revoke"
    audit_target = {
        "principal": name,
        "host": host,
        **{k: v for k, v in t.model_dump().items() if v},
    }
    await _execute(
        ctx, access, db, action=f"admin.privilege.{verb}", approval_op=f"admin.{verb}_privileges",
        target=audit_target, params={"privileges": sorted(p.upper() for p in body.privileges)},
        approval_id=approval_id,
        work=lambda: run_blocking(
            lambda: rel.change_privileges(
                name, host=host, grant=grant, privileges=body.privileges, target=target
            )
        ),
    )  # fmt: skip


@router.post("/principals/{name}/grants", status_code=204)
async def grant_privileges(  # noqa: PLR0913
    cid: uuid.UUID, name: str, body: PrivilegeChange, host: Host, db: DbSession,
    settings: AppSettings, ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> None:  # fmt: skip
    await _change(True, cid, name, body, host, db, settings, ctx, approval_id)


@router.delete("/principals/{name}/grants", status_code=204)
async def revoke_privileges(  # noqa: PLR0913
    cid: uuid.UUID, name: str, body: PrivilegeChange, host: Host, db: DbSession,
    settings: AppSettings, ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> None:  # fmt: skip
    await _change(False, cid, name, body, host, db, settings, ctx, approval_id)


async def _membership(  # noqa: PLR0913
    grant: bool, cid: uuid.UUID, name: str, body: MembershipBody, db: DbSession,
    settings: AppSettings, ctx: CurrentAuth, approval_id: uuid.UUID | None,
) -> None:  # fmt: skip
    access, rel = await _open(db, ctx, settings, cid, Perm.ADMIN_MANAGE_PRIVILEGES)
    require_confirmation(access, body.confirm, name)
    verb = "grant" if grant else "revoke"
    await _execute(
        ctx, access, db, action=f"admin.membership.{verb}", approval_op=f"admin.{verb}_privileges",
        target={"principal": name, "role": body.role}, params={"membership": True},
        approval_id=approval_id,
        work=lambda: run_blocking(lambda: rel.change_membership(name, body.role, grant=grant)),
    )  # fmt: skip


@router.post("/principals/{name}/memberships", status_code=204)
async def grant_membership(  # noqa: PLR0913
    cid: uuid.UUID, name: str, body: MembershipBody, db: DbSession, settings: AppSettings,
    ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> None:  # fmt: skip
    await _membership(True, cid, name, body, db, settings, ctx, approval_id)


@router.delete("/principals/{name}/memberships", status_code=204)
async def revoke_membership(  # noqa: PLR0913
    cid: uuid.UUID, name: str, body: MembershipBody, db: DbSession, settings: AppSettings,
    ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> None:  # fmt: skip
    await _membership(False, cid, name, body, db, settings, ctx, approval_id)


# ---------------------------------------------------------------------------- MongoDB


class RoleRef(Strict):
    role: str = Field(min_length=1, max_length=64)
    db: str | None = Field(default=None, max_length=255)


class MongoUserCreate(Strict):
    name: str = Field(min_length=1, max_length=64)
    password: SecretStr
    roles: list[RoleRef] = Field(min_length=1, max_length=20)


class MongoPassword(Strict):
    password: SecretStr


class MongoRoles(Strict):
    roles: list[RoleRef] = Field(min_length=1, max_length=20)
    confirm: str | None = Field(default=None, max_length=255)


class MongoPrivilege(Strict):
    db: str | None = Field(default=None, max_length=255)
    collection: str = Field(default="", max_length=255)
    actions: list[str] = Field(min_length=1, max_length=20)


class MongoRoleCreate(Strict):
    name: str = Field(min_length=1, max_length=64)
    privileges: list[MongoPrivilege] = Field(min_length=1, max_length=50)
    roles: list[RoleRef] = Field(default_factory=list, max_length=20)


class MongoPrivileges(Strict):
    privileges: list[MongoPrivilege] = Field(min_length=1, max_length=50)
    confirm: str | None = Field(default=None, max_length=255)


async def _mopen(
    db: DbSession, ctx: CurrentAuth, settings: AppSettings, cid: uuid.UUID, perm: Perm
) -> Any:
    access, adapter = await conns.open_adapter(db, ctx, settings, cid, perm, "document")
    return access, conns.as_document(adapter)


def _roles(items: list[RoleRef], database: str) -> list[dict[str, str]]:
    return [{"role": r.role, "db": r.db or database} for r in items]


def _privs(items: list[MongoPrivilege], database: str) -> list[dict[str, Any]]:
    return [
        {"resource": {"db": p.db or database, "collection": p.collection}, "actions": p.actions}
        for p in items
    ]


@mongo.get("/users")
async def mongo_users(
    cid: uuid.UUID, database: str, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> list[dict[str, Any]]:
    _, doc = await _mopen(db, ctx, settings, cid, Perm.ADMIN_VIEW_USERS)
    return await run_blocking(doc.list_users, database)


@mongo.post("/users", status_code=201)
async def mongo_create_user(  # noqa: PLR0913
    cid: uuid.UUID, database: str, body: MongoUserCreate, db: DbSession, settings: AppSettings,
    ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> dict[str, str]:  # fmt: skip
    access, doc = await _mopen(db, ctx, settings, cid, Perm.ADMIN_MANAGE_USERS)
    roles = _roles(body.roles, database)
    await _execute(
        ctx, access, db, action="admin.principal.create", approval_op="admin.create_principal",
        target={"database": database, "principal": body.name},
        params={"roles": roles, "password": "set"}, approval_id=approval_id,
        work=lambda: run_blocking(
            lambda: doc.create_user(database, body.name, body.password.get_secret_value(), roles)
        ),
    )  # fmt: skip
    return {"name": body.name}


@mongo.put("/users/{name}/password", status_code=204)
async def mongo_set_password(  # noqa: PLR0913
    cid: uuid.UUID, database: str, name: str, body: MongoPassword, db: DbSession,
    settings: AppSettings, ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> None:  # fmt: skip
    access, doc = await _mopen(db, ctx, settings, cid, Perm.ADMIN_MANAGE_USERS)
    await _execute(
        ctx, access, db, action="admin.principal.alter", approval_op="admin.alter_principal",
        target={"database": database, "principal": name}, params={"password": "set"},
        approval_id=approval_id,
        work=lambda: run_blocking(
            lambda: doc.set_user_password(database, name, body.password.get_secret_value())
        ),
    )  # fmt: skip


@mongo.delete("/users/{name}", status_code=204)
async def mongo_drop_user(  # noqa: PLR0913
    cid: uuid.UUID, database: str, name: str, body: DropBody, db: DbSession,
    settings: AppSettings, ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> None:  # fmt: skip
    access, doc = await _mopen(db, ctx, settings, cid, Perm.ADMIN_MANAGE_USERS)
    require_typed_name(body.confirm, name)
    await _execute(
        ctx, access, db, action="admin.principal.drop", approval_op="admin.drop_principal",
        target={"database": database, "principal": name}, params={}, approval_id=approval_id,
        work=lambda: run_blocking(doc.drop_user, database, name),
    )  # fmt: skip


async def _user_roles(  # noqa: PLR0913
    grant: bool, cid: uuid.UUID, database: str, name: str, body: MongoRoles, db: DbSession,
    settings: AppSettings, ctx: CurrentAuth, approval_id: uuid.UUID | None,
) -> None:  # fmt: skip
    access, doc = await _mopen(db, ctx, settings, cid, Perm.ADMIN_MANAGE_PRIVILEGES)
    require_confirmation(access, body.confirm, name)
    roles = _roles(body.roles, database)
    verb = "grant" if grant else "revoke"
    await _execute(
        ctx, access, db, action=f"admin.privilege.{verb}", approval_op=f"admin.{verb}_privileges",
        target={"database": database, "principal": name}, params={"roles": roles},
        approval_id=approval_id,
        work=lambda: run_blocking(
            lambda: doc.change_user_roles(database, name, roles, grant=grant)
        ),
    )  # fmt: skip


@mongo.post("/users/{name}/roles", status_code=204)
async def mongo_grant_roles(  # noqa: PLR0913
    cid: uuid.UUID, database: str, name: str, body: MongoRoles, db: DbSession,
    settings: AppSettings, ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> None:  # fmt: skip
    await _user_roles(True, cid, database, name, body, db, settings, ctx, approval_id)


@mongo.delete("/users/{name}/roles", status_code=204)
async def mongo_revoke_roles(  # noqa: PLR0913
    cid: uuid.UUID, database: str, name: str, body: MongoRoles, db: DbSession,
    settings: AppSettings, ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> None:  # fmt: skip
    await _user_roles(False, cid, database, name, body, db, settings, ctx, approval_id)


@mongo.get("/roles")
async def mongo_roles(
    cid: uuid.UUID, database: str, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> list[dict[str, Any]]:
    _, doc = await _mopen(db, ctx, settings, cid, Perm.ADMIN_VIEW_USERS)
    return await run_blocking(doc.list_roles, database)


@mongo.post("/roles", status_code=201)
async def mongo_create_role(  # noqa: PLR0913
    cid: uuid.UUID, database: str, body: MongoRoleCreate, db: DbSession, settings: AppSettings,
    ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> dict[str, str]:  # fmt: skip
    access, doc = await _mopen(db, ctx, settings, cid, Perm.ADMIN_MANAGE_PRIVILEGES)
    privs, inherited = _privs(body.privileges, database), _roles(body.roles, database)
    await _execute(
        ctx, access, db, action="admin.principal.create", approval_op="admin.create_principal",
        target={"database": database, "role": body.name},
        params={"privileges": privs, "roles": inherited}, approval_id=approval_id,
        work=lambda: run_blocking(
            lambda: doc.create_role(database, body.name, privs, inherited)
        ),
    )  # fmt: skip
    return {"name": body.name}


@mongo.delete("/roles/{role}", status_code=204)
async def mongo_drop_role(  # noqa: PLR0913
    cid: uuid.UUID, database: str, role: str, body: DropBody, db: DbSession,
    settings: AppSettings, ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> None:  # fmt: skip
    access, doc = await _mopen(db, ctx, settings, cid, Perm.ADMIN_MANAGE_PRIVILEGES)
    require_typed_name(body.confirm, role)
    await _execute(
        ctx, access, db, action="admin.principal.drop", approval_op="admin.drop_principal",
        target={"database": database, "role": role}, params={}, approval_id=approval_id,
        work=lambda: run_blocking(doc.drop_role, database, role),
    )  # fmt: skip


async def _role_privs(  # noqa: PLR0913
    grant: bool, cid: uuid.UUID, database: str, role: str, body: MongoPrivileges, db: DbSession,
    settings: AppSettings, ctx: CurrentAuth, approval_id: uuid.UUID | None,
) -> None:  # fmt: skip
    access, doc = await _mopen(db, ctx, settings, cid, Perm.ADMIN_MANAGE_PRIVILEGES)
    require_confirmation(access, body.confirm, role)
    privs = _privs(body.privileges, database)
    verb = "grant" if grant else "revoke"
    await _execute(
        ctx, access, db, action=f"admin.privilege.{verb}", approval_op=f"admin.{verb}_privileges",
        target={"database": database, "role": role}, params={"privileges": privs},
        approval_id=approval_id,
        work=lambda: run_blocking(
            lambda: doc.change_role_privileges(database, role, privs, grant=grant)
        ),
    )  # fmt: skip


@mongo.post("/roles/{role}/privileges", status_code=204)
async def mongo_grant_privileges(  # noqa: PLR0913
    cid: uuid.UUID, database: str, role: str, body: MongoPrivileges, db: DbSession,
    settings: AppSettings, ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> None:  # fmt: skip
    await _role_privs(True, cid, database, role, body, db, settings, ctx, approval_id)


@mongo.delete("/roles/{role}/privileges", status_code=204)
async def mongo_revoke_privileges(  # noqa: PLR0913
    cid: uuid.UUID, database: str, role: str, body: MongoPrivileges, db: DbSession,
    settings: AppSettings, ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> None:  # fmt: skip
    await _role_privs(False, cid, database, role, body, db, settings, ctx, approval_id)
