"""The application's only way to write to the audit log."""
from __future__ import annotations

import ipaddress

from django.http import HttpRequest

from audit.models import AuditEntry


def client_ip(request: HttpRequest) -> str | None:
    """The client's address, as Caddy worked it out: the peer, or behind a proxy named in `TRUSTED_PROXIES` the
    address that proxy saw (deploy/Caddyfile). Caddy always sets `X-Client-IP`, replacing any a client sent, and `web`
    publishes no port, so the header can only say what Caddy decided. `X-Forwarded-For` is not read: behind a trusted
    proxy, Caddy passes on the chain a client began. Without Caddy (development) the peer is the client."""
    for candidate in (request.headers.get("X-Client-IP", "").strip(), request.META.get("REMOTE_ADDR", "")):
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return None


def record(action: str, *, actor=None, target: str = "", before: dict | None = None, after: dict | None = None,
           request: HttpRequest | None = None) -> AuditEntry:
    return AuditEntry.objects.create(action=action, actor=actor, target=target, before=before, after=after,
                                     ip=client_ip(request) if request else None)
