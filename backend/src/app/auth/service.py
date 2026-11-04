"""Sessions, login (with lockout), MFA and first-run setup."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pyotp
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth import passwords
from app.auth.permissions import BUILTIN_ROLES, SUPER_ADMIN
from app.config import Settings
from app.errors import Conflict, Forbidden, NotFound, RateLimited, Unauthorized, ValidationFailed
from app.models import LoginAttempt, Role, SessionRow, SetupToken, User, utcnow
from app.services.runtime import get_envelope

INVALID_LOGIN = "Invalid credentials, or the account is temporarily locked."


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --------------------------------------------------------------------------- roles / setup


async def ensure_builtin_roles(db: AsyncSession) -> None:
    """Built-in roles are defined in code and re-synced at every start."""
    existing = {r.name: r for r in (await db.execute(select(Role))).scalars()}
    for name, (desc, perms) in BUILTIN_ROLES.items():
        role = existing.get(name)
        values = sorted(p.value for p in perms)
        if role is None:
            db.add(Role(name=name, description=desc, builtin=True, permissions=values))
        elif role.builtin and (role.permissions != values or role.description != desc):
            role.permissions, role.description = values, desc
    await db.flush()


async def ensure_setup_token(db: AsyncSession) -> str | None:
    """Create the one-time bootstrap token when no users exist. Returns it only to the
    replica that created it (so it is logged exactly once)."""
    if (await db.execute(select(func.count()).select_from(User))).scalar_one() > 0:
        return None
    token = secrets.token_urlsafe(32)
    stmt = (
        pg_insert(SetupToken)
        .values(id=1, token_hash=sha256(token), created_at=utcnow())
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(SetupToken.id)
    )
    created = (await db.execute(stmt)).scalar_one_or_none()
    return token if created else None


async def reset_setup_token(db: AsyncSession) -> str | None:
    if (await db.execute(select(func.count()).select_from(User))).scalar_one() > 0:
        return None
    await db.execute(delete(SetupToken))
    return await ensure_setup_token(db)


async def setup_required(db: AsyncSession) -> bool:
    return (await db.execute(select(func.count()).select_from(User))).scalar_one() == 0


async def complete_setup(
    db: AsyncSession,
    settings: Settings,
    *,
    token: str,
    username: str,
    password: str,
    email: str | None,
    display_name: str,
    ip: str | None,
) -> User:
    row = (
        await db.execute(select(SetupToken).where(SetupToken.id == 1).with_for_update())
    ).scalar_one_or_none()
    if (
        row is None
        or row.used_at is not None
        or not hmac.compare_digest(row.token_hash, sha256(token))
        or not await setup_required(db)
    ):
        await audit.record("auth.setup", actor=audit.Actor(ip=ip), result="denied")
        raise Forbidden("Setup token is invalid or setup has already been completed.")
    username = username.strip().lower()
    passwords.check_policy(password, username=username, min_length=settings.password_min_length)
    role = (await db.execute(select(Role).where(Role.name == SUPER_ADMIN))).scalar_one()
    user = User(
        username=username,
        display_name=display_name or username,
        email=email,
        password_hash=passwords.hash_password(password),
        role_id=role.id,
    )
    db.add(user)
    row.used_at = utcnow()
    await db.flush()
    await audit.record(
        "auth.setup",
        actor=audit.Actor(user.id, username, ip),
        target={"user": username},
    )
    return user


# --------------------------------------------------------------------------- sessions


def _new_token() -> str:
    return secrets.token_urlsafe(32)


async def create_session(
    db: AsyncSession,
    settings: Settings,
    user: User,
    *,
    mfa_state: str,
    ip: str | None,
    user_agent: str | None,
) -> tuple[SessionRow, str]:
    token = _new_token()
    now = utcnow()
    row = SessionRow(
        token_hash=sha256(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(24),
        mfa_state=mfa_state,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=settings.session_absolute_hours),
        ip=ip,
        user_agent=(user_agent or "")[:400] or None,
    )
    db.add(row)
    await db.flush()
    return row, token


async def rotate_session(db: AsyncSession, row: SessionRow, *, mfa_state: str) -> str:
    """Issue a new token for the same session (privilege change -> new identifier)."""
    token = _new_token()
    row.token_hash = sha256(token)
    row.csrf_token = secrets.token_urlsafe(24)
    row.mfa_state = mfa_state
    await db.flush()
    return token


async def load_session(db: AsyncSession, settings: Settings, token: str) -> SessionRow | None:
    row = (
        await db.execute(select(SessionRow).where(SessionRow.token_hash == sha256(token)))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None or not row.user.is_active:
        return None
    now = utcnow()
    if now >= row.expires_at or now - row.last_seen_at > timedelta(
        minutes=settings.session_idle_minutes
    ):
        return None
    if now - row.last_seen_at > timedelta(seconds=30):
        row.last_seen_at = now
    return row


async def revoke_user_sessions(
    db: AsyncSession, user_id: uuid.UUID, *, except_id: uuid.UUID | None = None
) -> int:
    stmt = (
        update(SessionRow)
        .where(SessionRow.user_id == user_id, SessionRow.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    if except_id:
        stmt = stmt.where(SessionRow.id != except_id)
    result = cast("CursorResult[Any]", await db.execute(stmt))
    return result.rowcount or 0


# --------------------------------------------------------------------------- login


@dataclass
class LoginResult:
    session: SessionRow
    token: str
    user: User


async def _lock_on_failure(db: AsyncSession, settings: Settings, user: User) -> None:
    user.failed_login_count += 1
    over = user.failed_login_count - settings.login_max_failures
    if over >= 0:
        seconds = min(settings.login_lock_base_seconds * (2**over), settings.login_lock_max_seconds)
        user.locked_until = utcnow() + timedelta(seconds=seconds)


async def login(
    db: AsyncSession,
    settings: Settings,
    *,
    username: str,
    password: str,
    ip: str,
    user_agent: str | None,
) -> LoginResult:
    username = username.strip().lower()
    window = utcnow() - timedelta(seconds=settings.login_ip_window_seconds)
    recent_failures = (
        await db.execute(
            select(func.count())
            .select_from(LoginAttempt)
            .where(LoginAttempt.ip == ip, LoginAttempt.ts > window, LoginAttempt.success.is_(False))
        )
    ).scalar_one()
    if recent_failures >= settings.login_ip_max_attempts:
        await audit.record(
            "auth.login",
            actor=audit.Actor(None, username, ip),
            result="denied",
            detail={"reason": "ip_rate_limited"},
        )
        raise RateLimited("Too many failed sign-in attempts from this address. Try again later.")

    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    ok = passwords.verify_password(user.password_hash if user else None, password)
    now = utcnow()
    locked = bool(user and user.locked_until and user.locked_until > now)
    reason = None
    if user is None or not ok:
        reason = "bad_credentials"
    elif locked:
        reason = "account_locked"
    elif not user.is_active:
        reason = "account_inactive"

    db.add(LoginAttempt(ip=ip, username=username[:100], success=reason is None))
    if reason or user is None:
        if user is not None and reason == "bad_credentials":
            await _lock_on_failure(db, settings, user)
        await db.flush()
        await audit.record(
            "auth.login",
            actor=audit.Actor(user.id if user else None, username, ip),
            result="failure",
            detail={"reason": reason},
        )
        # Commit the failure counters even though we raise.
        await db.commit()
        raise Unauthorized(INVALID_LOGIN)

    assert user is not None
    user.failed_login_count = 0
    user.locked_until = None
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
    if user.mfa_enabled:
        state = "pending_verify"
    elif user.role.require_mfa:
        state = "pending_enroll"
    else:
        state = "ok"
    if state == "ok":
        user.last_login_at = now
    row, token = await create_session(
        db, settings, user, mfa_state=state, ip=ip, user_agent=user_agent
    )
    await audit.record(
        "auth.login", actor=audit.Actor(user.id, username, ip), detail={"mfa_state": state}
    )
    return LoginResult(row, token, user)


# --------------------------------------------------------------------------- MFA


def _totp_accept(secret: str, code: str, last_step: int) -> int | None:
    """Return the accepted time step (for replay prevention) or None."""
    code = code.strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return None
    totp = pyotp.TOTP(secret)
    step_now = int(time.time()) // 30
    for step in (step_now, step_now - 1, step_now + 1):
        if step > last_step and hmac.compare_digest(totp.at(step * 30), code):
            return step
    return None


def new_recovery_codes() -> tuple[list[str], list[str]]:
    plain = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(10)]
    return plain, [sha256(c) for c in plain]


async def verify_mfa_code(db: AsyncSession, settings: Settings, user: User, code: str) -> bool:
    """Check a TOTP or recovery code. Applies the same lockout as password failures."""
    if user.locked_until and user.locked_until > utcnow():
        return False
    ok = False
    if user.mfa_secret:
        secret = get_envelope().decrypt(user.mfa_secret, f"mfa:{user.id}").decode()
        step = _totp_accept(secret, code, user.mfa_last_step)
        if step is not None:
            user.mfa_last_step = step
            ok = True
    if not ok:
        digest = sha256(code.strip().lower())
        if digest in user.recovery_codes:
            user.recovery_codes = [c for c in user.recovery_codes if c != digest]
            ok = True
    if ok:
        user.failed_login_count = 0
    else:
        await _lock_on_failure(db, settings, user)
    return ok


async def start_enrollment(db: AsyncSession, settings: Settings, user: User) -> dict[str, str]:
    secret = pyotp.random_base32()
    user.mfa_pending_secret = get_envelope().encrypt(secret.encode(), f"mfa:{user.id}")
    await db.flush()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name=settings.mfa_issuer)
    return {"secret": secret, "otpauth_uri": uri}


async def confirm_enrollment(db: AsyncSession, user: User, code: str) -> list[str]:
    if not user.mfa_pending_secret:
        raise ValidationFailed("Start MFA enrollment first.")
    secret = get_envelope().decrypt(user.mfa_pending_secret, f"mfa:{user.id}").decode()
    step = _totp_accept(secret, code, 0)
    if step is None:
        raise ValidationFailed("The verification code is not valid.")
    plain, hashed = new_recovery_codes()
    user.mfa_secret, user.mfa_pending_secret = user.mfa_pending_secret, None
    user.mfa_enabled, user.mfa_last_step, user.recovery_codes = True, step, hashed
    await db.flush()
    return plain


async def reset_mfa(db: AsyncSession, user: User) -> None:
    user.mfa_enabled, user.mfa_secret, user.mfa_pending_secret = False, None, None
    user.recovery_codes, user.mfa_last_step = [], 0
    await revoke_user_sessions(db, user.id)


# --------------------------------------------------------------------------- passwords


async def change_password(
    db: AsyncSession,
    settings: Settings,
    user: User,
    *,
    current: str,
    new: str,
    keep_session: uuid.UUID | None,
) -> None:
    if not passwords.verify_password(user.password_hash, current):
        raise Forbidden("Current password is incorrect.")
    if passwords.verify_password(user.password_hash, new):
        raise ValidationFailed("The new password must differ from the current one.")
    passwords.check_policy(new, username=user.username, min_length=settings.password_min_length)
    user.password_hash = passwords.hash_password(new)
    user.password_changed_at = utcnow()
    user.must_change_password = False
    await revoke_user_sessions(db, user.id, except_id=keep_session)


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFound("User not found.")
    return user


async def ensure_unique_username(db: AsyncSession, username: str) -> None:
    exists = (
        await db.execute(select(func.count()).select_from(User).where(User.username == username))
    ).scalar_one()
    if exists:
        raise Conflict("A user with this username already exists.")
