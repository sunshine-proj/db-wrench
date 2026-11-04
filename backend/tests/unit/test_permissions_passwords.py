import pytest

from app.auth import passwords
from app.auth.permissions import (
    APP_SCOPE,
    BUILTIN_ROLES,
    CONNECTION_SCOPE,
    WRITE_PERMS,
    Perm,
    effective_connection_permissions,
)
from app.errors import ValidationFailed


def test_scopes_partition_the_catalogue():
    assert set(Perm) == APP_SCOPE | CONNECTION_SCOPE
    assert not (APP_SCOPE & CONNECTION_SCOPE)


def test_viewer_cannot_write_and_editor_can():
    viewer = BUILTIN_ROLES["Viewer"][1]
    editor = BUILTIN_ROLES["Editor"][1]
    assert Perm.DATA_READ in viewer and not (viewer & WRITE_PERMS)
    assert Perm.DATA_UPDATE in editor


def test_read_only_connection_strips_every_write_permission():
    perms = effective_connection_permissions(
        {p.value for p in Perm}, read_only=True, is_super_admin=True
    )
    assert not (perms & {p.value for p in WRITE_PERMS})
    assert Perm.DATA_READ.value in perms


def test_app_scope_permissions_never_leak_into_connection_permissions():
    perms = effective_connection_permissions(
        {Perm.APP_MANAGE_USERS.value, Perm.DATA_READ.value}, read_only=False, is_super_admin=False
    )
    assert perms == {Perm.DATA_READ.value}


def test_no_grant_means_no_permissions():
    assert (
        effective_connection_permissions(set(), read_only=False, is_super_admin=False)
        == frozenset()
    )


def test_database_admin_is_not_super_admin():
    assert BUILTIN_ROLES["Database Admin"][1] != BUILTIN_ROLES["Super Admin"][1]
    assert Perm.APP_MANAGE_USERS not in BUILTIN_ROLES["Database Admin"][1]


def test_argon2id_hash_and_verify():
    h = passwords.hash_password("Correct-Horse-9")
    assert h.startswith("$argon2id$")
    assert passwords.verify_password(h, "Correct-Horse-9")
    assert not passwords.verify_password(h, "wrong")
    assert not passwords.verify_password(None, "anything")  # unknown user path


@pytest.mark.parametrize(
    "pw",
    [
        "short1A!",
        "alllowercaseletters",
        "NoDigitsOrSymbols",
        "aaaaaaaaaaaaaaaaaaaaA1",
        "has-bob-1A-in-it-xx",
    ],
)
def test_password_policy_rejects_weak(pw):
    with pytest.raises(ValidationFailed):
        passwords.check_policy(pw, username="bob", min_length=12)


def test_password_policy_accepts_strong():
    passwords.check_policy("Tr0ub4dor&Horse-battery", username="alice", min_length=12)
