"""Application database models (users, RBAC, connections, audit, settings)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    permissions: Mapped[list[str]] = mapped_column(JSONB, default=list)
    require_mfa: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(100), unique=True)  # stored lowercase
    display_name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)

    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_secret: Mapped[str | None] = mapped_column(Text, nullable=True)  # envelope-encrypted
    mfa_pending_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    mfa_last_step: Mapped[int] = mapped_column(BigInteger, default=0)
    recovery_codes: Mapped[list[str]] = mapped_column(JSONB, default=list)  # sha256 hashes

    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    role: Mapped[Role] = relationship(lazy="joined")


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    csrf_token: Mapped[str] = mapped_column(String(64))
    # ok | pending_verify (MFA code needed) | pending_enroll (role requires MFA)
    mfa_state: Mapped[str] = mapped_column(String(20), default="ok")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)

    user: Mapped[User] = relationship(lazy="joined")


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_ip_ts", "ip", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ip: Mapped[str] = mapped_column(String(64))
    username: Mapped[str] = mapped_column(String(100))
    success: Mapped[bool] = mapped_column(Boolean)


class SetupToken(Base):
    """Singleton row: the one-time first-run bootstrap token (stored hashed)."""

    __tablename__ = "setup_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    # postgresql | mysql | mariadb | mssql | clickhouse | sqlite | redis | elasticsearch | mongodb
    db_type: Mapped[str] = mapped_column(String(20))
    environment: Mapped[str] = mapped_column(String(20), default="development")
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str] = mapped_column(String(255))
    secret: Mapped[str] = mapped_column(Text)  # envelope-encrypted JSON {"password": ...}
    credential_model: Mapped[str] = mapped_column(String(20), default="shared")
    tls_mode: Mapped[str] = mapped_column(String(20), default="disable")
    tls_ca: Mapped[str | None] = mapped_column(Text, nullable=True)
    connect_timeout_s: Mapped[int] = mapped_column(Integer, default=10)
    pool_max: Mapped[int] = mapped_column(Integer, default=10)
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ConnectionGrant(Base):
    __tablename__ = "connection_grants"
    __table_args__ = (UniqueConstraint("user_id", "connection_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))

    role: Mapped[Role] = relationship(lazy="joined")


class AuditEvent(Base):
    """Append-only, hash-chained. UPDATE/DELETE/TRUNCATE are blocked by DB triggers."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_ts", "ts"),
        Index("ix_audit_actor", "actor_user_id"),
        Index("ix_audit_connection", "connection_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    actor_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    target: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[str] = mapped_column(String(20))  # success | failure | denied
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class SensitiveMarker(Base):
    """A column (relational) or field path (MongoDB) whose values are masked for users that
    lack ``data.read_sensitive``. ``schema`` is "" when the engine has no schema level."""

    __tablename__ = "sensitive_markers"
    __table_args__ = (
        UniqueConstraint("connection_id", "database", "schema", "object", "field"),
        Index("ix_sensitive_conn_obj", "connection_id", "database", "object"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE")
    )
    database: Mapped[str] = mapped_column(String(255))
    schema: Mapped[str] = mapped_column(String(255), default="")
    object: Mapped[str] = mapped_column(String(255))
    field: Mapped[str] = mapped_column(String(500))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Operation(Base):
    """A running or finished user-initiated query. The backend reference (PostgreSQL pid,
    MySQL thread id, MongoDB comment) lets *any* replica cancel it."""

    __tablename__ = "operations"
    __table_args__ = (Index("ix_operations_user_status", "user_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(10))  # sql | mongo
    status: Mapped[str] = mapped_column(String(12), default="running")
    database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    backend_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QueryHistory(Base):
    __tablename__ = "query_history"
    __table_args__ = (Index("ix_query_history_user_ts", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(10))
    database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    schema: Mapped[str | None] = mapped_column(String(255), nullable=True)
    statement: Mapped[str] = mapped_column(Text)  # literals redacted when so configured
    redacted: Mapped[bool] = mapped_column(Boolean, default=False)
    statement_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(12))
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SavedQuery(Base):
    __tablename__ = "saved_queries"
    __table_args__ = (UniqueConstraint("owner_id", "connection_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    schema: Mapped[str | None] = mapped_column(String(255), nullable=True)
    statement: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(10), default="private")  # private | shared
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ApiToken(Base):
    """Scoped, expiring automation token. Only the SHA-256 of the secret is stored; the
    effective permissions are the intersection of ``scopes`` and the owner's own rights."""

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(16))  # display only, e.g. "dbw_a1b2c3d4"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    connection_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(lazy="joined")


class ApprovalPolicy(Base):
    """Operations that need a second person's approval. Scoped to one connection *or* to an
    environment (a connection-specific policy replaces the environment one)."""

    __tablename__ = "approval_policies"
    __table_args__ = (
        CheckConstraint(
            "(connection_id IS NULL) <> (environment IS NULL)", name="ck_policy_one_scope"
        ),
        Index(
            "uq_policy_connection",
            "connection_id",
            unique=True,
            postgresql_where="connection_id IS NOT NULL",
        ),
        Index(
            "uq_policy_environment",
            "environment",
            unique=True,
            postgresql_where="environment IS NOT NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), nullable=True
    )
    environment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    operations: Mapped[list[str]] = mapped_column(JSONB, default=list)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Approval(Base):
    """A request to run one specific operation. Bound to the requester, the operation and a
    digest of its parameters; single use; expires."""

    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_status_created", "status", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    requester_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    operation: Mapped[str] = mapped_column(String(60))
    params_digest: Mapped[str] = mapped_column(String(64))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # what the approver sees
    # pending | approved | rejected | executed | cancelled
    status: Mapped[str] = mapped_column(String(12), default="pending")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserIdentity(Base):
    """Link between a DB Wrench user and an external (OIDC) subject."""

    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("provider", "subject"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(20))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OidcLoginState(Base):
    """Server-side state of an in-flight OIDC login (so any replica can finish it)."""

    __tablename__ = "oidc_login_states"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(64))
    code_verifier: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScopeGrant(Base):
    """A role granted on part of a connection: a database, a schema or a table. Adds the role's
    permissions for objects inside that scope; deny by default everywhere else."""

    __tablename__ = "scope_grants"
    __table_args__ = (UniqueConstraint("user_id", "connection_id", "database", "schema", "object"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    database: Mapped[str] = mapped_column(String(255))
    schema: Mapped[str] = mapped_column(String(255), default="")  # "" = whole database
    object: Mapped[str] = mapped_column(String(255), default="")  # "" = whole schema/database
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))

    role: Mapped[Role] = relationship(lazy="joined")


class UserCredential(Base):
    """A user's own login for a ``credential_model = per_user`` connection (envelope-encrypted,
    bound to the user and the connection by the encryption context)."""

    __tablename__ = "user_credentials"
    __table_args__ = (UniqueConstraint("user_id", "connection_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    username: Mapped[str] = mapped_column(String(255))
    secret: Mapped[str] = mapped_column(Text)  # envelope-encrypted JSON {"password": ...}
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
