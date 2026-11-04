"""Application user and role management."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.auth import user_out
from app.api.schemas import (
    DirectoryUser,
    PasswordReset,
    RoleCreate,
    RoleOut,
    RoleUpdate,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.audit import service as audit
from app.auth import service
from app.auth import users as users_svc
from app.auth.deps import AppSettings, AuthContext, CurrentAuth, DbSession, require_app_permission
from app.auth.permissions import ALL_PERMISSIONS, SUPER_ADMIN, Perm
from app.errors import Forbidden
from app.models import Role, User

router = APIRouter(tags=["users"])
ManageUsers = Annotated[AuthContext, Depends(require_app_permission(Perm.APP_MANAGE_USERS))]
ManageRoles = Annotated[AuthContext, Depends(require_app_permission(Perm.APP_MANAGE_ROLES))]


def role_out(r: Role) -> RoleOut:
    return RoleOut(
        id=r.id,
        name=r.name,
        description=r.description,
        builtin=r.builtin,
        permissions=sorted(r.permissions),
        require_mfa=r.require_mfa,
    )


@router.get("/users", response_model=list[UserOut])
async def list_users(db: DbSession, _: ManageUsers) -> list[UserOut]:
    rows = (await db.execute(select(User).order_by(User.username))).scalars().all()
    return [user_out(u) for u in rows]


@router.post("/users", status_code=201, response_model=UserOut)
async def create_user(
    body: UserCreate, db: DbSession, settings: AppSettings, ctx: ManageUsers
) -> UserOut:
    user = await users_svc.create_user(
        db,
        settings,
        ctx,
        username=body.username,
        password=body.password.get_secret_value(),
        role_id=body.role_id,
        email=body.email,
        display_name=body.display_name,
    )
    await audit.record(
        "user.create", actor=ctx.actor, target={"user": user.username, "role": user.role.name}
    )
    return user_out(user)


@router.get("/directory/users", response_model=list[DirectoryUser])
async def user_directory(db: DbSession, ctx: CurrentAuth) -> list[DirectoryUser]:
    """Minimal id/name list used to pick users when granting connection access. Available to
    anyone who can manage access on connections (not only user administrators)."""
    allowed = (
        ctx.is_super_admin
        or ctx.has_app(Perm.APP_MANAGE_USERS)
        or Perm.CONNECTION_MANAGE_ACCESS.value in ctx.user.role.permissions
    )
    if not allowed:
        raise Forbidden("You are not allowed to list users.")
    rows = (
        (await db.execute(select(User).where(User.is_active.is_(True)).order_by(User.username)))
        .scalars()
        .all()
    )
    return [DirectoryUser(id=u.id, username=u.username, display_name=u.display_name) for u in rows]


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(user_id: uuid.UUID, db: DbSession, _: ManageUsers) -> UserOut:
    return user_out(await service.get_user(db, user_id))


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID, body: UserUpdate, db: DbSession, ctx: ManageUsers
) -> UserOut:
    user = await service.get_user(db, user_id)
    changes = await users_svc.update_user(
        db,
        ctx,
        user,
        display_name=body.display_name,
        email=body.email,
        role_id=body.role_id,
        is_active=body.is_active,
    )
    await audit.record(
        "user.update", actor=ctx.actor, target={"user": user.username}, detail=changes
    )
    return user_out(user)


@router.post("/users/{user_id}/password-reset", status_code=204)
async def reset_password(
    user_id: uuid.UUID, body: PasswordReset, db: DbSession, settings: AppSettings, ctx: ManageUsers
) -> None:
    user = await service.get_user(db, user_id)
    await users_svc.admin_reset_password(
        db, settings, ctx, user, body.new_password.get_secret_value()
    )
    await audit.record("user.password_reset", actor=ctx.actor, target={"user": user.username})


@router.post("/users/{user_id}/mfa/reset", status_code=204)
async def reset_user_mfa(user_id: uuid.UUID, db: DbSession, ctx: ManageUsers) -> None:
    user = await service.get_user(db, user_id)
    if user.role.name == SUPER_ADMIN and not ctx.is_super_admin:
        raise Forbidden("Only a Super Admin can reset a Super Admin's MFA.")
    await service.reset_mfa(db, user)
    await audit.record("user.mfa_reset", actor=ctx.actor, target={"user": user.username})


@router.delete("/users/{user_id}/sessions", status_code=204)
async def revoke_user_sessions(user_id: uuid.UUID, db: DbSession, ctx: ManageUsers) -> None:
    user = await service.get_user(db, user_id)
    count = await service.revoke_user_sessions(db, user.id)
    await audit.record(
        "user.sessions_revoked",
        actor=ctx.actor,
        target={"user": user.username},
        detail={"count": count},
    )


# ---------------------------------------------------------------------------- roles


@router.get("/roles", response_model=list[RoleOut])
async def list_roles(db: DbSession, _: CurrentAuth) -> list[RoleOut]:
    rows = (await db.execute(select(Role).order_by(Role.builtin.desc(), Role.name))).scalars().all()
    return [role_out(r) for r in rows]


@router.get("/permissions", response_model=list[str])
async def list_permissions(_: CurrentAuth) -> list[str]:
    return sorted(ALL_PERMISSIONS)


@router.post("/roles", status_code=201, response_model=RoleOut)
async def create_role(body: RoleCreate, db: DbSession, ctx: ManageRoles) -> RoleOut:
    role = await users_svc.create_role(
        db,
        ctx,
        name=body.name,
        description=body.description,
        permissions=body.permissions,
        require_mfa=body.require_mfa,
    )
    await audit.record(
        "role.create",
        actor=ctx.actor,
        target={"role": role.name},
        detail={"permissions": role.permissions},
    )
    return role_out(role)


@router.patch("/roles/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: uuid.UUID, body: RoleUpdate, db: DbSession, ctx: ManageRoles
) -> RoleOut:
    role = await users_svc.get_role(db, role_id)
    await users_svc.update_role(
        db,
        ctx,
        role,
        description=body.description,
        permissions=body.permissions,
        require_mfa=body.require_mfa,
    )
    await audit.record(
        "role.update",
        actor=ctx.actor,
        target={"role": role.name},
        detail={"permissions": role.permissions, "require_mfa": role.require_mfa},
    )
    return role_out(role)


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(role_id: uuid.UUID, db: DbSession, ctx: ManageRoles) -> None:
    role = await users_svc.get_role(db, role_id)
    await users_svc.delete_role(db, role)
    await audit.record("role.delete", actor=ctx.actor, target={"role": role.name})
