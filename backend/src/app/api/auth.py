"""Authentication endpoints: setup, login/logout, MFA, password, sessions."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

from app.api.schemas import (
    LoginRequest,
    LoginResponse,
    MfaCodeRequest,
    MfaDisableRequest,
    PasswordChangeRequest,
    SessionInfo,
    SessionListItem,
    SetupRequest,
    SetupStatus,
    UserOut,
)
from app.audit import service as audit
from app.auth import passwords, service
from app.auth.deps import (
    AppSettings,
    AuthContext,
    DbSession,
    auth_pw_ok,
    client_ip,
    session_ctx,
)
from app.errors import Conflict, Forbidden, NotFound, Unauthorized
from app.models import SessionRow, User, utcnow

router = APIRouter(prefix="/auth", tags=["auth"])


def user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        username=u.username,
        display_name=u.display_name,
        email=u.email,
        role_id=u.role_id,
        role=u.role.name,
        is_active=u.is_active,
        mfa_enabled=u.mfa_enabled,
        must_change_password=u.must_change_password,
        last_login_at=u.last_login_at,
        created_at=u.created_at,
    )


def _set_cookie(response: Response, settings: AppSettings, token: str) -> None:
    response.set_cookie(
        settings.cookie_name,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


@router.get("/setup-status", response_model=SetupStatus)
async def setup_status(db: DbSession) -> SetupStatus:
    return SetupStatus(setup_required=await service.setup_required(db))


@router.post("/setup", status_code=201, response_model=UserOut)
async def setup(
    body: SetupRequest, request: Request, db: DbSession, settings: AppSettings
) -> UserOut:
    user = await service.complete_setup(
        db,
        settings,
        token=body.token,
        username=body.username,
        password=body.password.get_secret_value(),
        email=body.email,
        display_name=body.display_name,
        ip=client_ip(request, settings),
    )
    await db.refresh(user, ["role"])
    return user_out(user)


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest, request: Request, response: Response, db: DbSession, settings: AppSettings
) -> LoginResponse:
    result = await service.login(
        db,
        settings,
        username=body.username,
        password=body.password.get_secret_value(),
        ip=client_ip(request, settings),
        user_agent=request.headers.get("user-agent"),
    )
    _set_cookie(response, settings, result.token)
    return LoginResponse(
        mfa_state=result.session.mfa_state,
        csrf_token=result.session.csrf_token,
        user=user_out(result.user),
    )


@router.post("/logout", status_code=204)
async def logout(
    response: Response,
    db: DbSession,
    settings: AppSettings,
    ctx: Annotated[AuthContext, Depends(session_ctx)],
) -> None:
    ctx.session.revoked_at = utcnow()
    response.delete_cookie(settings.cookie_name, path="/")
    await audit.record("auth.logout", actor=ctx.actor)


@router.get("/session", response_model=SessionInfo)
async def current_session(ctx: Annotated[AuthContext, Depends(session_ctx)]) -> SessionInfo:
    return SessionInfo(
        user=user_out(ctx.user),
        csrf_token=ctx.session.csrf_token,
        mfa_state=ctx.session.mfa_state,
        app_permissions=sorted(ctx.app_permissions),
        password_change_required=ctx.user.must_change_password,
    )


# ----------------------------------------------------------------------------------- MFA


@router.post("/mfa/verify", response_model=LoginResponse)
async def mfa_verify(
    body: MfaCodeRequest,
    response: Response,
    db: DbSession,
    settings: AppSettings,
    ctx: Annotated[AuthContext, Depends(session_ctx)],
) -> LoginResponse:
    if ctx.session.mfa_state != "pending_verify":
        raise Conflict("No MFA verification is pending for this session.")
    ok = await service.verify_mfa_code(db, settings, ctx.user, body.code)
    if not ok:
        await audit.record("auth.mfa.verify", actor=ctx.actor, result="failure")
        await db.commit()
        raise Unauthorized("The verification code is not valid.")
    ctx.user.last_login_at = utcnow()
    token = await service.rotate_session(db, ctx.session, mfa_state="ok")
    _set_cookie(response, settings, token)
    await audit.record("auth.mfa.verify", actor=ctx.actor)
    return LoginResponse(mfa_state="ok", csrf_token=ctx.session.csrf_token, user=user_out(ctx.user))


@router.post("/mfa/enroll")
async def mfa_enroll(
    db: DbSession, settings: AppSettings, ctx: Annotated[AuthContext, Depends(session_ctx)]
) -> dict[str, str]:
    if ctx.session.mfa_state == "pending_verify":
        raise Forbidden("Verify your current MFA code first.")
    if ctx.user.mfa_enabled:
        raise Conflict("MFA is already enabled. Disable it before enrolling again.")
    return await service.start_enrollment(db, settings, ctx.user)


@router.post("/mfa/enroll/confirm")
async def mfa_confirm(
    body: MfaCodeRequest,
    response: Response,
    db: DbSession,
    settings: AppSettings,
    ctx: Annotated[AuthContext, Depends(session_ctx)],
) -> dict[str, object]:
    if ctx.session.mfa_state == "pending_verify":
        raise Forbidden("Verify your current MFA code first.")
    codes = await service.confirm_enrollment(db, ctx.user, body.code)
    if ctx.session.mfa_state == "pending_enroll":
        ctx.user.last_login_at = utcnow()
        _set_cookie(
            response, settings, await service.rotate_session(db, ctx.session, mfa_state="ok")
        )
    await audit.record("auth.mfa.enroll", actor=ctx.actor)
    return {"recovery_codes": codes, "csrf_token": ctx.session.csrf_token}


@router.post("/mfa/disable", status_code=204)
async def mfa_disable(
    body: MfaDisableRequest,
    db: DbSession,
    settings: AppSettings,
    ctx: Annotated[AuthContext, Depends(auth_pw_ok)],
) -> None:
    user = ctx.user
    if user.role.require_mfa:
        raise Forbidden("Your role requires multi-factor authentication.")
    if not passwords.verify_password(user.password_hash, body.password.get_secret_value()):
        raise Forbidden("Password is incorrect.")
    if not await service.verify_mfa_code(db, settings, user, body.code):
        await db.commit()
        raise Forbidden("The verification code is not valid.")
    await service.reset_mfa(db, user)
    await audit.record("auth.mfa.disable", actor=ctx.actor)


# ----------------------------------------------------------------------------- password


@router.post("/password", status_code=204)
async def change_password(
    body: PasswordChangeRequest,
    db: DbSession,
    settings: AppSettings,
    ctx: Annotated[AuthContext, Depends(auth_pw_ok)],
) -> None:
    await service.change_password(
        db,
        settings,
        ctx.user,
        current=body.current_password.get_secret_value(),
        new=body.new_password.get_secret_value(),
        keep_session=ctx.session.id,
    )
    await audit.record("auth.password.change", actor=ctx.actor)


# ----------------------------------------------------------------------------- sessions


@router.get("/sessions", response_model=list[SessionListItem])
async def list_sessions(
    db: DbSession, ctx: Annotated[AuthContext, Depends(auth_pw_ok)]
) -> list[SessionListItem]:
    rows = (
        await db.execute(
            select(SessionRow)
            .where(SessionRow.user_id == ctx.user.id, SessionRow.revoked_at.is_(None))
            .order_by(SessionRow.last_seen_at.desc())
        )
    ).scalars()
    return [
        SessionListItem(
            id=s.id,
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
            ip=s.ip,
            user_agent=s.user_agent,
            current=s.id == ctx.session.id,
        )
        for s in rows
        if s.expires_at > utcnow()
    ]


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(
    session_id: uuid.UUID, db: DbSession, ctx: Annotated[AuthContext, Depends(auth_pw_ok)]
) -> None:
    row = await db.get(SessionRow, session_id)
    if row is None or row.user_id != ctx.user.id:
        raise NotFound("Session not found.")
    row.revoked_at = utcnow()
    await audit.record("auth.session.revoke", actor=ctx.actor, target={"session": str(session_id)})
