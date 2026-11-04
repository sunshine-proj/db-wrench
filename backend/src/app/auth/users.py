"""Application user and role management (with privilege-escalation guards)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import passwords, service
from app.auth.deps import AuthContext
from app.auth.permissions import ALL_PERMISSIONS, SUPER_ADMIN
from app.config import Settings
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.models import ConnectionGrant, Role, User, utcnow


async def get_role(db: AsyncSession, role_id: uuid.UUID) -> Role:
    role = await db.get(Role, role_id)
    if role is None:
        raise NotFound("Role not found.")
    return role


def _assert_can_assign(ctx: AuthContext, role: Role) -> None:
    """Non-super-admins may only hand out roles that are a subset of their own rights."""
    if ctx.is_super_admin:
        return
    if role.name == SUPER_ADMIN or not set(role.permissions) <= set(ctx.user.role.permissions):
        raise Forbidden("You cannot assign a role with more permissions than your own.")


async def _active_super_admins(db: AsyncSession) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(User)
            .join(Role, User.role_id == Role.id)
            .where(Role.name == SUPER_ADMIN, User.is_active.is_(True))
        )
    ).scalar_one()


async def create_user(
    db: AsyncSession,
    settings: Settings,
    ctx: AuthContext,
    *,
    username: str,
    password: str,
    role_id: uuid.UUID,
    email: str | None,
    display_name: str,
) -> User:
    username = username.strip().lower()
    if not username.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise ValidationFailed("Username may contain letters, digits, '.', '_' and '-' only.")
    role = await get_role(db, role_id)
    _assert_can_assign(ctx, role)
    await service.ensure_unique_username(db, username)
    passwords.check_policy(password, username=username, min_length=settings.password_min_length)
    user = User(
        username=username,
        display_name=display_name or username,
        email=email,
        password_hash=passwords.hash_password(password),
        role_id=role.id,
        must_change_password=True,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user, ["role"])
    return user


async def update_user(
    db: AsyncSession,
    ctx: AuthContext,
    user: User,
    *,
    display_name: str | None,
    email: str | None,
    role_id: uuid.UUID | None,
    is_active: bool | None,
) -> dict[str, object]:
    changes: dict[str, object] = {}
    target_is_super = user.role.name == SUPER_ADMIN
    if target_is_super and not ctx.is_super_admin:
        raise Forbidden("Only a Super Admin can modify a Super Admin.")
    if display_name is not None:
        user.display_name = display_name
    if email is not None:
        user.email = email or None
    if role_id is not None and role_id != user.role_id:
        role = await get_role(db, role_id)
        _assert_can_assign(ctx, role)
        if target_is_super and role.name != SUPER_ADMIN and await _active_super_admins(db) <= 1:
            raise Conflict("At least one active Super Admin is required.")
        changes["role"] = role.name
        user.role_id = role.id
        await db.flush()
        await db.refresh(user, ["role"])
        await service.revoke_user_sessions(db, user.id)
    if is_active is not None and is_active != user.is_active:
        if not is_active and user.id == ctx.user.id:
            raise Conflict("You cannot deactivate your own account.")
        if not is_active and target_is_super and await _active_super_admins(db) <= 1:
            raise Conflict("At least one active Super Admin is required.")
        user.is_active = is_active
        changes["is_active"] = is_active
        if not is_active:
            await service.revoke_user_sessions(db, user.id)
    return changes


async def admin_reset_password(
    db: AsyncSession, settings: Settings, ctx: AuthContext, user: User, new_password: str
) -> None:
    if user.role.name == SUPER_ADMIN and not ctx.is_super_admin:
        raise Forbidden("Only a Super Admin can reset a Super Admin's password.")
    passwords.check_policy(
        new_password, username=user.username, min_length=settings.password_min_length
    )
    user.password_hash = passwords.hash_password(new_password)
    user.password_changed_at = utcnow()
    user.must_change_password = True
    user.failed_login_count, user.locked_until = 0, None
    await service.revoke_user_sessions(db, user.id)


# ------------------------------------------------------------------------------------- roles


def validate_permissions(perms: list[str], ctx: AuthContext) -> list[str]:
    unknown = sorted(set(perms) - ALL_PERMISSIONS)
    if unknown:
        raise ValidationFailed(f"Unknown permissions: {', '.join(unknown)}")
    if not ctx.is_super_admin and not set(perms) <= set(ctx.user.role.permissions):
        raise Forbidden("You cannot grant permissions you do not hold.")
    return sorted(set(perms))


async def create_role(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    name: str,
    description: str,
    permissions: list[str],
    require_mfa: bool,
) -> Role:
    perms = validate_permissions(permissions, ctx)
    if (await db.execute(select(Role).where(Role.name == name))).scalar_one_or_none():
        raise Conflict("A role with this name already exists.")
    role = Role(
        name=name,
        description=description,
        permissions=perms,
        builtin=False,
        require_mfa=require_mfa,
    )
    db.add(role)
    await db.flush()
    return role


async def update_role(
    db: AsyncSession,
    ctx: AuthContext,
    role: Role,
    *,
    description: str | None,
    permissions: list[str] | None,
    require_mfa: bool | None,
) -> None:
    if role.builtin and permissions is not None:
        raise Conflict("Built-in role permissions cannot be changed; create a Custom role.")
    if description is not None:
        role.description = description
    if permissions is not None:
        role.permissions = validate_permissions(permissions, ctx)
    if require_mfa is not None:
        role.require_mfa = require_mfa


async def delete_role(db: AsyncSession, role: Role) -> None:
    if role.builtin:
        raise Conflict("Built-in roles cannot be deleted.")
    in_use = (
        await db.execute(select(func.count()).select_from(User).where(User.role_id == role.id))
    ).scalar_one() + (
        await db.execute(
            select(func.count())
            .select_from(ConnectionGrant)
            .where(ConnectionGrant.role_id == role.id)
        )
    ).scalar_one()
    if in_use:
        raise Conflict("The role is still assigned to users or connection grants.")
    await db.delete(role)
