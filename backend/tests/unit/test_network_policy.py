import ipaddress

import pytest

from app.connections.network import NetworkPolicy, validate_policy_input
from app.errors import NetworkPolicyViolation

PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


def check(policy: NetworkPolicy, ip: str, host: str = "db.internal") -> None:
    policy.check_address(ipaddress.ip_address(ip), host)


@pytest.mark.security
@pytest.mark.parametrize(
    "ip",
    [
        "169.254.169.254",  # AWS/GCP/Azure metadata
        "169.254.0.1",
        "100.100.100.200",  # Alibaba
        "192.0.0.192",  # Oracle
        "fd00:ec2::254",  # AWS IPv6 IMDS
        "fe80::1",
        "0.0.0.0",
        "224.0.0.1",
        "::ffff:169.254.169.254",  # IPv4-mapped bypass attempt
    ],
)
def test_never_permitted_ranges_are_denied_even_if_allowlisted(ip):
    policy = NetworkPolicy(
        allow_cidrs=("0.0.0.0/0", "::/0"), allow_hosts=("*",), allow_loopback=True
    )
    with pytest.raises(NetworkPolicyViolation):
        check(policy, ip)


@pytest.mark.security
@pytest.mark.parametrize("ip", ["127.0.0.1", "127.1.2.3", "::1", "::ffff:127.0.0.1"])
def test_loopback_denied_by_default(ip):
    with pytest.raises(NetworkPolicyViolation):
        check(NetworkPolicy(allow_cidrs=("0.0.0.0/0", "::/0")), ip)


def test_loopback_allowed_only_with_explicit_flag():
    check(NetworkPolicy(allow_loopback=True), "127.0.0.1")


def test_private_ranges_allowed_public_denied_by_default():
    policy = NetworkPolicy(allow_cidrs=PRIVATE)
    check(policy, "10.1.2.3")
    check(policy, "192.168.5.5")
    with pytest.raises(NetworkPolicyViolation):
        check(policy, "8.8.8.8")


def test_host_pattern_allows_public_address():
    policy = NetworkPolicy(allow_cidrs=PRIVATE, allow_hosts=("*.db.example.com",))
    check(policy, "93.184.216.34", "primary.db.example.com")
    with pytest.raises(NetworkPolicyViolation):
        check(policy, "93.184.216.34", "evil.example.org")


def test_resolve_validates_every_answer(monkeypatch):
    import socket

    def fake(host, port, type=0):  # noqa: A002
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    with pytest.raises(NetworkPolicyViolation):  # one bad answer poisons the lookup
        NetworkPolicy(allow_cidrs=PRIVATE).resolve("rebind.example", 5432)


def test_resolve_returns_pinned_addresses(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda h, p, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", p))],
    )
    assert NetworkPolicy(allow_cidrs=PRIVATE).resolve("db", 5432) == ["10.0.0.5"]


def test_unresolvable_host_is_a_policy_violation():
    with pytest.raises(NetworkPolicyViolation):
        NetworkPolicy(allow_cidrs=PRIVATE).resolve("does-not-exist.invalid", 5432)


def test_fingerprint_changes_with_policy():
    a = NetworkPolicy(allow_cidrs=PRIVATE).fingerprint()
    assert a != NetworkPolicy(allow_cidrs=PRIVATE[:1]).fingerprint()


def test_policy_input_validation():
    validate_policy_input(["10.0.0.0/8"], ["*.example.com"])
    with pytest.raises(ValueError):
        validate_policy_input(["not-a-cidr"], [])
    with pytest.raises(ValueError):
        validate_policy_input([], ["bad host/with slash"])
