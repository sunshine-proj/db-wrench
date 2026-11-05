"""OIDC single sign-on endpoints (public login / callback) and its administration."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, SecretStr

from app.api.auth import _set_cookie
from app.api.schemas import Strict
from app.audit import service as audit
from app.audit.service import Actor
from app.auth import oidc
from app.auth.deps import AppSettings, AuthContext, DbSession, client_ip, require_app_permission
from app.auth.permissions import Perm
from app.errors import AppError

router = APIRouter(prefix="/auth/oidc", tags=["sso"])
settings_router = APIRouter(tags=["sso"])

CanManageSettings = Annotated[
    AuthContext, Depends(require_app_permission(Perm.APP_MANAGE_SETTINGS))
]


class SsoPublic(BaseModel):
    enabled: bool
    display_name: str


class RoleMapping(Strict):
    group: str = Field(min_length=1, max_length=255)
    role: str = Field(min_length=1, max_length=100)


class OidcIn(Strict):
    enabled: bool
    issuer: str = Field(default="", max_length=500)
    client_id: str = Field(default="", max_length=255)
    client_secret: SecretStr | None = Field(
        default=None, description="Write-only; omit to keep the stored secret."
    )
    scopes: str = Field(default="openid email profile", max_length=200)
    groups_claim: str = Field(default="groups", max_length=100)
    username_claim: str = Field(default="preferred_username", max_length=100)
    display_name: str = Field(default="Single sign-on", max_length=60)
    role_mappings: list[RoleMapping] = Field(default_factory=list, max_length=200)
    default_role: str | None = Field(default=None, max_length=100)


class OidcOut(BaseModel):
    enabled: bool
    issuer: str
    client_id: str
    client_secret_set: bool
    scopes: str
    groups_claim: str
    username_claim: str
    display_name: str
    role_mappings: list[RoleMapping]
    default_role: str | None
    redirect_uri: str | None


def _out(cfg: oidc.OidcConfig, settings: AppSettings) -> OidcOut:
    return OidcOut(
        enabled=cfg.enabled,
        issuer=cfg.issuer,
        client_id=cfg.client_id,
        client_secret_set=bool(cfg.client_secret),
        scopes=cfg.scopes,
        groups_claim=cfg.groups_claim,
        username_claim=cfg.username_claim,
        display_name=cfg.display_name,
        role_mappings=[RoleMapping(**m) for m in cfg.role_mappings],
        default_role=cfg.default_role,
        redirect_uri=oidc.redirect_uri(settings) if settings.public_url else None,
    )


# ------------------------------------------------------------------------ login flow


@router.get("/config", response_model=SsoPublic)
async def public_config(db: DbSession) -> SsoPublic:
    cfg = await oidc.load_config(db)
    return SsoPublic(enabled=cfg.enabled, display_name=cfg.display_name)


@router.get("/login")
async def login(db: DbSession, settings: AppSettings) -> RedirectResponse:
    url = await oidc.begin_login(db, settings)
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})


@router.get("/callback")
async def callback(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    code: Annotated[str | None, Query(max_length=2048)] = None,
    state: Annotated[str | None, Query(max_length=256)] = None,
    error: Annotated[str | None, Query(max_length=100)] = None,
) -> RedirectResponse:
    ip = client_ip(request, settings)
    try:
        if error or not code or not state:
            raise oidc.SsoDenied("The identity provider did not complete the login.")
        user, _, token, created = await oidc.finish_login(
            db, settings, code=code, state=state, ip=ip,
            user_agent=request.headers.get("user-agent"),
        )  # fmt: skip
    except AppError as exc:
        await audit.record(
            "auth.sso.login", actor=Actor(ip=ip), result="denied", detail={"reason": exc.detail}
        )
        return RedirectResponse(
            "/login?sso_error=denied", status_code=303, headers={"Cache-Control": "no-store"}
        )
    actor = Actor(user.id, user.username, ip)
    if created:
        await audit.record(
            "auth.sso.provision",
            actor=actor,
            target={"user": user.username, "role": user.role.name},
        )
    await audit.record("auth.sso.login", actor=actor, detail={"role": user.role.name})
    response = RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store"})
    _set_cookie(response, settings, token)
    return response


# ---------------------------------------------------------------------- administration


@settings_router.get("/settings/oidc", response_model=OidcOut)
async def get_oidc(db: DbSession, settings: AppSettings, _: CanManageSettings) -> OidcOut:
    return _out(await oidc.load_config(db), settings)


@settings_router.put("/settings/oidc", response_model=OidcOut)
async def put_oidc(
    body: OidcIn, db: DbSession, settings: AppSettings, ctx: CanManageSettings
) -> OidcOut:
    cfg = oidc.OidcConfig(
        enabled=body.enabled,
        issuer=body.issuer.strip(),
        client_id=body.client_id.strip(),
        client_secret=body.client_secret.get_secret_value() if body.client_secret else "",
        scopes=body.scopes.strip(),
        groups_claim=body.groups_claim.strip(),
        username_claim=body.username_claim.strip(),
        display_name=body.display_name.strip(),
        role_mappings=[m.model_dump() for m in body.role_mappings],
        default_role=body.default_role,
    )
    await oidc.save_config(db, settings, ctx.user.id, cfg, keep_secret=body.client_secret is None)
    await audit.record(
        "settings.oidc.update",
        actor=ctx.actor,
        detail={
            "enabled": cfg.enabled, "issuer": cfg.issuer, "client_id": cfg.client_id,
            "secret_changed": body.client_secret is not None,
            "mappings": len(cfg.role_mappings), "default_role": cfg.default_role,
        },
    )  # fmt: skip
    return _out(await oidc.load_config(db), settings)
