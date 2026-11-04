"""Permission catalogue and built-in roles.

A role is a set of permission keys. Permissions have a *scope*:

* ``app`` permissions come from the user's own role (e.g. managing users, viewing audit).
* ``connection`` permissions come from the role the user was granted on a specific
  connection (Super Admin implicitly holds everything on every connection).

Effective permissions on a connection are further reduced by connection flags
(read-only strips every write permission).
"""

from __future__ import annotations

from enum import StrEnum


class Perm(StrEnum):
    # connection scope
    CONNECTION_VIEW = "connection.view"
    CONNECTION_EDIT = "connection.edit"
    CONNECTION_DELETE = "connection.delete"
    CONNECTION_TEST = "connection.test"
    CONNECTION_MANAGE_ACCESS = "connection.manage_access"
    METADATA_VIEW = "metadata.view"
    DATA_READ = "data.read"
    DATA_READ_SENSITIVE = "data.read_sensitive"
    DATA_INSERT = "data.insert"
    DATA_UPDATE = "data.update"
    DATA_DELETE = "data.delete"
    DATA_BULK_WRITE = "data.bulk_write"
    QUERY_EXECUTE_READ = "query.execute_read"
    QUERY_EXECUTE_WRITE = "query.execute_write"
    QUERY_EXECUTE_SCRIPT = "query.execute_script"
    QUERY_VIEW_HISTORY = "query.view_history"
    QUERY_MANAGE_SAVED = "query.manage_saved"
    EXPORT_EXPORT = "export.export"
    EXPORT_IMPORT = "export.import"
    ADMIN_MANAGE_DATABASES = "admin.manage_databases"
    ADMIN_MANAGE_SCHEMAS = "admin.manage_schemas"
    ADMIN_MANAGE_INDEXES = "admin.manage_indexes"
    ADMIN_MANAGE_USERS = "admin.manage_users"
    ADMIN_MANAGE_PRIVILEGES = "admin.manage_privileges"
    # app scope
    CONNECTION_CREATE = "connection.create"
    AUDIT_VIEW = "audit.view"
    AUDIT_EXPORT = "audit.export"
    APP_MANAGE_USERS = "app.manage_users"
    APP_MANAGE_ROLES = "app.manage_roles"
    APP_MANAGE_SETTINGS = "app.manage_settings"
    APP_MANAGE_TOKENS = "app.manage_tokens"


APP_SCOPE: frozenset[Perm] = frozenset(
    {
        Perm.CONNECTION_CREATE,
        Perm.AUDIT_VIEW,
        Perm.AUDIT_EXPORT,
        Perm.APP_MANAGE_USERS,
        Perm.APP_MANAGE_ROLES,
        Perm.APP_MANAGE_SETTINGS,
        Perm.APP_MANAGE_TOKENS,
    }
)
CONNECTION_SCOPE: frozenset[Perm] = frozenset(set(Perm) - APP_SCOPE)

# Removed from the effective set when a connection is read-only.
WRITE_PERMS: frozenset[Perm] = frozenset(
    {
        Perm.DATA_INSERT,
        Perm.DATA_UPDATE,
        Perm.DATA_DELETE,
        Perm.DATA_BULK_WRITE,
        Perm.QUERY_EXECUTE_WRITE,
        Perm.QUERY_EXECUTE_SCRIPT,
        Perm.EXPORT_IMPORT,
        Perm.ADMIN_MANAGE_DATABASES,
        Perm.ADMIN_MANAGE_SCHEMAS,
        Perm.ADMIN_MANAGE_INDEXES,
        Perm.ADMIN_MANAGE_USERS,
        Perm.ADMIN_MANAGE_PRIVILEGES,
    }
)

_VIEWER = {
    Perm.CONNECTION_VIEW,
    Perm.METADATA_VIEW,
    Perm.DATA_READ,
    Perm.QUERY_EXECUTE_READ,
    Perm.QUERY_VIEW_HISTORY,
}
_EDITOR = _VIEWER | {
    Perm.DATA_INSERT,
    Perm.DATA_UPDATE,
    Perm.DATA_DELETE,
    Perm.QUERY_EXECUTE_WRITE,
    Perm.QUERY_MANAGE_SAVED,
    Perm.EXPORT_EXPORT,
}
_DB_ADMIN = _EDITOR | CONNECTION_SCOPE | {Perm.CONNECTION_CREATE, Perm.AUDIT_VIEW}

SUPER_ADMIN = "Super Admin"
BUILTIN_ROLES: dict[str, tuple[str, frozenset[Perm]]] = {
    SUPER_ADMIN: ("Full application access", frozenset(Perm)),
    "Database Admin": ("Manage assigned connections and databases", frozenset(_DB_ADMIN)),
    "Editor": ("Read and modify authorized data", frozenset(_EDITOR)),
    "Viewer": ("Read-only access", frozenset(_VIEWER)),
}
ALL_PERMISSIONS: frozenset[str] = frozenset(p.value for p in Perm)


def effective_connection_permissions(
    granted: set[str] | frozenset[str], *, read_only: bool, is_super_admin: bool
) -> frozenset[str]:
    perms = set(ALL_PERMISSIONS if is_super_admin else granted)
    perms &= {p.value for p in CONNECTION_SCOPE}
    if read_only:
        perms -= {p.value for p in WRITE_PERMS}
    return frozenset(perms)
