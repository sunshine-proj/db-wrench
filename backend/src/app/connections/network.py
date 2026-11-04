"""Outbound network destination policy (SSRF protection).

Rules, in order:

1. Every address the host resolves to is validated (DNS answers are never trusted
   individually).
2. Link-local, cloud-metadata, unspecified and multicast ranges are *always* denied.
3. Loopback is denied unless the deployment explicitly enables it (development/test only).
4. Otherwise an address must fall in an allowed CIDR, or the hostname must match an
   allowed host pattern.

Callers connect to the *pinned* address returned here, not to the hostname, so a later DNS
change (rebinding) cannot redirect an already-validated connection.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import socket
from dataclasses import dataclass, field

from app.errors import NetworkPolicyViolation

IPNet = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddr = ipaddress.IPv4Address | ipaddress.IPv6Address

ALWAYS_DENY: tuple[IPNet, ...] = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "169.254.0.0/16",  # link-local, includes 169.254.169.254 metadata
        "100.100.100.200/32",  # Alibaba Cloud metadata
        "192.0.0.192/32",  # Oracle Cloud metadata
        "224.0.0.0/4",
        "::/128",
        "fe80::/10",
        "fd00:ec2::254/128",  # AWS IMDS over IPv6
        "ff00::/8",
    )
)
LOOPBACK: tuple[IPNet, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


@dataclass(frozen=True)
class NetworkPolicy:
    allow_cidrs: tuple[str, ...] = ()
    allow_hosts: tuple[str, ...] = ()
    allow_loopback: bool = False
    _nets: tuple[IPNet, ...] = field(default=(), init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        nets = tuple(ipaddress.ip_network(c, strict=False) for c in self.allow_cidrs)
        object.__setattr__(self, "_nets", nets)

    def fingerprint(self) -> str:
        return f"{sorted(self.allow_cidrs)}|{sorted(self.allow_hosts)}|{self.allow_loopback}"

    # -- checks ---------------------------------------------------------------------------

    def check_address(self, addr: IPAddr, hostname: str) -> None:
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if any(addr in n for n in ALWAYS_DENY):
            raise NetworkPolicyViolation(f"{addr} is in a range that is never permitted")
        if any(addr in n for n in LOOPBACK):
            if self.allow_loopback:
                return
            raise NetworkPolicyViolation("loopback addresses are not permitted")
        if any(addr in n for n in self._nets):
            return
        if self._host_allowed(hostname):
            return
        raise NetworkPolicyViolation(
            f"{addr} is not covered by the allowed CIDR ranges or host patterns"
        )

    def _host_allowed(self, hostname: str) -> bool:
        h = hostname.lower().rstrip(".")
        return any(fnmatch.fnmatchcase(h, p.lower()) for p in self.allow_hosts)

    def resolve(self, host: str, port: int) -> list[str]:
        """Resolve ``host`` and validate every answer. Returns pinned IP strings."""
        if not host or len(host) > 255:
            raise NetworkPolicyViolation("invalid host")
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise NetworkPolicyViolation(f"host {host!r} could not be resolved") from exc
        addrs: list[str] = []
        for info in infos:
            raw = str(info[4][0])
            addr = ipaddress.ip_address(raw.split("%", 1)[0])
            self.check_address(addr, host)
            if str(addr) not in addrs:
                addrs.append(str(addr))
        if not addrs:
            raise NetworkPolicyViolation(f"host {host!r} did not resolve")
        return addrs


def validate_policy_input(cidrs: list[str], hosts: list[str]) -> None:
    for c in cidrs:
        try:
            ipaddress.ip_network(c, strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid CIDR {c!r}") from exc
    for h in hosts:
        if not h or len(h) > 255 or any(ch.isspace() or ch == "/" for ch in h):
            raise ValueError(f"invalid host pattern {h!r}")
