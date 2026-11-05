"""Request / response models (the public API contract)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Header
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
)

from app.database.core.base import DbType, TlsMode

Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=100)]
Environment = Literal["production", "staging", "development", "other"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------------ auth


class SetupStatus(BaseModel):
    setup_required: bool


class SetupRequest(Strict):
    token: str = Field(min_length=10, max_length=200)
    username: Username
    password: SecretStr
    email: EmailStr | None = None
    display_name: str = Field(default="", max_length=200)


class LoginRequest(Strict):
    username: str = Field(min_length=1, max_length=100)
    password: SecretStr


class MfaCodeRequest(Strict):
    code: str = Field(min_length=6, max_length=32)


class PasswordChangeRequest(Strict):
    current_password: SecretStr
    new_password: SecretStr


class MfaDisableRequest(Strict):
    password: SecretStr
    code: str = Field(min_length=6, max_length=32)


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str
    email: str | None
    role_id: uuid.UUID
    role: str
    is_active: bool
    mfa_enabled: bool
    must_change_password: bool
    last_login_at: datetime | None
    created_at: datetime


class DirectoryUser(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str


class SessionInfo(BaseModel):
    authenticated: bool = True
    user: UserOut
    csrf_token: str
    mfa_state: str
    app_permissions: list[str]
    password_change_required: bool


class LoginResponse(BaseModel):
    mfa_state: str
    csrf_token: str
    user: UserOut


class SessionListItem(BaseModel):
    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    ip: str | None
    user_agent: str | None
    current: bool


# ------------------------------------------------------------------------------ users / roles


class UserCreate(Strict):
    username: Username
    password: SecretStr
    role_id: uuid.UUID
    email: EmailStr | None = None
    display_name: str = Field(default="", max_length=200)


class UserUpdate(Strict):
    display_name: str | None = Field(default=None, max_length=200)
    email: EmailStr | None = None
    role_id: uuid.UUID | None = None
    is_active: bool | None = None


class PasswordReset(Strict):
    new_password: SecretStr


class RoleOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    builtin: bool
    permissions: list[str]
    require_mfa: bool


class RoleCreate(Strict):
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=500)
    permissions: list[str]
    require_mfa: bool = False


class RoleUpdate(Strict):
    description: str | None = Field(default=None, max_length=500)
    permissions: list[str] | None = None
    require_mfa: bool | None = None


# ------------------------------------------------------------------------------ connections

_NAME_RE = re.compile(r"^[\w .\-/()]{2,120}$")


def _check_name(v: str) -> str:
    v = v.strip()
    if not _NAME_RE.match(v):
        raise ValueError("name must be 2-120 characters (letters, digits, space . - _ / ( ))")
    return v


def _check_host(v: str) -> str:
    v = v.strip()
    if any(c.isspace() or c in "/@\\" for c in v):
        raise ValueError("host must be a hostname or IP address")
    return v


def _check_ca(v: str | None) -> str | None:
    if v and "BEGIN CERTIFICATE" not in v:
        raise ValueError("tls_ca must be a PEM certificate")
    return v or None


def _check_options(v: dict[str, Any] | None) -> dict[str, Any] | None:
    if v is None:
        return v
    extra = set(v) - {"auth_source"}
    if extra:
        raise ValueError(f"unsupported options: {', '.join(sorted(extra))}")
    if "auth_source" in v and not (isinstance(v["auth_source"], str) and v["auth_source"]):
        raise ValueError("auth_source must be a non-empty string")
    return v


class ConnectionBase(Strict):
    name: str
    environment: Environment = "development"
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    database: str | None = Field(default=None, max_length=255)
    username: str = Field(default="", max_length=255)
    tls_mode: TlsMode = TlsMode.DISABLE
    tls_ca: str | None = Field(default=None, max_length=20000)
    connect_timeout_s: int = Field(default=10, ge=1, le=60)
    pool_max: int = Field(default=10, ge=1, le=50)
    read_only: bool = True
    enabled: bool = True
    credential_model: Literal["shared", "per_user"] = "shared"
    options: dict[str, Any] = Field(default_factory=dict)

    _v_name = field_validator("name")(_check_name)
    _v_host = field_validator("host")(_check_host)
    _v_ca = field_validator("tls_ca")(_check_ca)
    _v_options = field_validator("options")(_check_options)


class ConnectionCreate(ConnectionBase):
    db_type: DbType
    password: SecretStr = SecretStr("")


class ConnectionUpdate(Strict):
    name: str | None = None
    environment: Environment | None = None
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = None
    username: str | None = Field(default=None, max_length=255)
    password: SecretStr | None = None
    tls_mode: TlsMode | None = None
    tls_ca: str | None = None
    connect_timeout_s: int | None = Field(default=None, ge=1, le=60)
    pool_max: int | None = Field(default=None, ge=1, le=50)
    read_only: bool | None = None
    enabled: bool | None = None
    credential_model: Literal["shared", "per_user"] | None = None
    options: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str | None) -> str | None:
        return None if v is None else _check_name(v)

    @field_validator("host")
    @classmethod
    def _v_host(cls, v: str | None) -> str | None:
        return None if v is None else _check_host(v)

    _v_ca = field_validator("tls_ca")(_check_ca)
    _v_options = field_validator("options")(_check_options)


class ConnectionOut(BaseModel):
    """Never contains the password or any other secret."""

    id: uuid.UUID
    name: str
    db_type: DbType
    environment: str
    host: str
    port: int
    database: str | None
    username: str
    has_password: bool
    credential_model: str
    tls_mode: str
    has_tls_ca: bool
    connect_timeout_s: int
    pool_max: int
    read_only: bool
    enabled: bool
    options: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    permissions: list[str]
    capabilities: dict[str, Any]


class ConnectionTestOut(BaseModel):
    ok: bool
    server_version: str | None = None
    latency_ms: float | None = None
    error: str | None = None
    error_code: str | None = None


class GrantItem(Strict):
    user_id: uuid.UUID
    role_id: uuid.UUID


class GrantOut(BaseModel):
    user_id: uuid.UUID
    username: str
    role_id: uuid.UUID
    role: str


class GrantsUpdate(Strict):
    grants: list[GrantItem]


class NetworkPolicyIn(Strict):
    allow_cidrs: list[str] = Field(max_length=200)
    allow_hosts: list[str] = Field(max_length=200)


class NetworkPolicyOut(BaseModel):
    allow_cidrs: list[str]
    allow_hosts: list[str]
    allow_loopback: bool
    always_denied: list[str]


ApprovalId = Annotated[
    uuid.UUID | None,
    Header(alias="Approval-Id", description="Id of an approved request for this operation."),
]


class CredentialsIn(Strict):
    username: str = Field(min_length=1, max_length=255)
    password: SecretStr


class CredentialsOut(BaseModel):
    """Never contains the password."""

    required: bool
    configured: bool
    username: str | None


class ScopeGrantIn(Strict):
    user_id: uuid.UUID
    role_id: uuid.UUID
    database: str = Field(min_length=1, max_length=255)
    schema_name: str = Field(default="", alias="schema", max_length=255)
    object: str = Field(default="", max_length=255)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ScopeGrantsUpdate(Strict):
    grants: list[ScopeGrantIn] = Field(max_length=2000)


class ScopeGrantOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    user_id: uuid.UUID
    username: str
    role_id: uuid.UUID
    role: str
    database: str
    schema_name: str = Field(alias="schema")
    object: str
