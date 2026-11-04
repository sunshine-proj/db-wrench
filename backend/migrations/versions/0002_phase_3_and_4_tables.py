"""phase 3 and 4 tables

Revision ID: 0002
Revises: 0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("database", sa.String(length=255), nullable=True),
        sa.Column("backend_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_operations_user_status", "operations", ["user_id", "status"], unique=False)
    op.create_table(
        "query_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("database", sa.String(length=255), nullable=True),
        sa.Column("schema", sa.String(length=255), nullable=True),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.Column("statement_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("error_code", sa.String(length=60), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_query_history_user_ts", "query_history", ["user_id", "created_at"], unique=False
    )
    op.create_table(
        "saved_queries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("database", sa.String(length=255), nullable=True),
        sa.Column("schema", sa.String(length=255), nullable=True),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("visibility", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "connection_id", "name"),
    )
    op.create_index(
        op.f("ix_saved_queries_connection_id"), "saved_queries", ["connection_id"], unique=False
    )
    op.create_table(
        "sensitive_markers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("database", sa.String(length=255), nullable=False),
        sa.Column("schema", sa.String(length=255), nullable=False),
        sa.Column("object", sa.String(length=255), nullable=False),
        sa.Column("field", sa.String(length=500), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "database", "schema", "object", "field"),
    )
    op.create_index(
        "ix_sensitive_conn_obj",
        "sensitive_markers",
        ["connection_id", "database", "object"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_sensitive_conn_obj", table_name="sensitive_markers")
    op.drop_table("sensitive_markers")
    op.drop_index(op.f("ix_saved_queries_connection_id"), table_name="saved_queries")
    op.drop_table("saved_queries")
    op.drop_index("ix_query_history_user_ts", table_name="query_history")
    op.drop_table("query_history")
    op.drop_index("ix_operations_user_status", table_name="operations")
    op.drop_table("operations")
