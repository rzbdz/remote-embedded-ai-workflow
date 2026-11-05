"""registry module: a directory of board hosts that announce themselves.

Board hosts running bsp-hostd with `server.announce` POST their id, port, addresses and ssh user
here every minute or so. The address the announcement came from is the one this machine sees,
so it is tried first. Clients with `registry` in the site's remotes.json (or $BSP_REGISTRY) list
the fresh entries as remotes, so new hosts and changed addresses need no client configuration.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from ..core.cmdspec import CommandSpec, Param
from ..core.commands import Command
from ..core.errors import InvalidRequest, NotFound
from ..core.module import MODULES, Module, ModuleContext

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_SSH_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _address(text: Any) -> str | None:
    try:
        ip = ipaddress.ip_address(str(text).strip().strip("[]"))
    except ValueError:
        return None
    return None if ip.is_loopback or ip.is_unspecified or ip.is_link_local else str(ip)


def _url(addr: str, port: int) -> str:
    return f"http://[{addr}]:{port}" if ":" in addr else f"http://{addr}:{port}"


@MODULES.register("registry")
class RegistryModule(Module):
    name = "registry"
    title = "Host registry"

    def __init__(self, ctx: ModuleContext, config: dict[str, Any]):
        super().__init__(ctx, config)
        self.path: Path = ctx.module_state_dir("registry") / "hosts.json"
        self.stale_s = float(config.get("stale_s", 300))
        self.trust_proxy = bool(config.get("trust_proxy", False))
        self._lock = threading.Lock()
        try:
            self._hosts: dict[str, dict[str, Any]] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self._hosts = {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._hosts, indent=2))
        tmp.replace(self.path)

    # ----------------------------------------------------------- announce --
    def announce(self, body: dict[str, Any], seen: str | None) -> dict[str, Any]:
        host = str(body.get("host") or "")
        if not _ID.match(host):
            raise InvalidRequest(f"invalid host id {host!r}", hint="letters, digits, _ . - (max 63)")
        try:
            port = int(body.get("port", 8765))
        except (TypeError, ValueError):
            port = 0
        if not 0 < port < 65536:
            raise InvalidRequest("port must be 1-65535")
        user = body.get("ssh_user")
        if user is not None and not _SSH_USER.match(str(user)):
            raise InvalidRequest(f"invalid ssh user {user!r}")
        seen = _address(seen)
        addrs = [a for a in (_address(x) for x in (body.get("addresses") or [])[:16]) if a]
        addrs = list(dict.fromkeys(([seen] if seen else []) + addrs))
        if not addrs:
            raise InvalidRequest("no usable address in the announcement",
                                 hint="announce from the host's network address, or list addresses")
        now = time.time()
        with self._lock:
            old = self._hosts.get(host) or {}
            entry = {"host": host, "port": port, "addresses": addrs, "seen_address": seen, "ssh_user": user,
                     "description": str(body.get("description") or "")[:200],
                     "version": str(body.get("version") or "")[:40],
                     "duts": [str(d) for d in (body.get("duts") or [])][:200],
                     "first_seen": old.get("first_seen", now), "seen": now}
            changed = {k: v for k, v in entry.items() if k not in ("seen", "first_seen") and old.get(k) != v}
            self._hosts[host] = entry
            self._save()
        if changed:
            self.ctx.bus.publish("registry.announced", host=host, addresses=addrs, new=not old)
        return {"host": host, "registered": True, "seen_address": seen, "addresses": addrs,
                "refresh_s": max(10, int(self.stale_s / 4))}

    # -------------------------------------------------------------- reads --
    def view(self, e: dict[str, Any]) -> dict[str, Any]:
        age = time.time() - e["seen"]
        first, *rest = e["addresses"]
        remote = {"url": _url(first, e["port"]), "fallback_urls": [_url(a, e["port"]) for a in rest],
                  "ssh": f"{e['ssh_user']}@{first}" if e.get("ssh_user") else None, "port": e["port"],
                  "description": e.get("description") or f"registered {e['host']}"}
        return {**e, "age_s": round(age), "stale": age > self.stale_s, "remote": remote}

    def list(self, all: bool = False) -> list[dict[str, Any]]:   # noqa: A002 - CLI flag name
        with self._lock:
            items = [self.view(e) for _, e in sorted(self._hosts.items())]
        return items if all else [e for e in items if not e["stale"]]

    def forget(self, host: str) -> dict[str, Any]:
        with self._lock:
            if self._hosts.pop(host, None) is None:
                raise NotFound(f"host {host!r} is not registered", hint="see `bsp registry list --all`")
            self._save()
        self.ctx.bus.publish("registry.forgotten", host=host)
        return {"host": host, "forgotten": True}

    def status(self) -> dict[str, Any]:
        items = self.list(all=True)
        return {"hosts": len(items), "fresh": sum(not e["stale"] for e in items)}

    def commands(self) -> list[Command]:
        return [
            Command(CommandSpec("registry.list", "Board hosts that announced themselves here, with how to reach them",
                                [Param("all", "boolean", "include hosts not heard from recently", required=False,
                                       default=False)], mutating=False),
                    self.list,
                    lambda items: "\n".join(
                        f"{e['host']:<16} {e['remote']['url']:<28} {'STALE ' if e['stale'] else ''}"
                        f"seen {e['age_s']}s ago  duts: {', '.join(e['duts']) or '-'}" for e in items)
                    or "(no hosts registered)"),
            Command(CommandSpec("registry.forget", "Remove a host from the registry (it comes back if it announces again)",
                                [Param("host", help="host id")]),
                    self.forget, lambda r: f"forgot {r['host']}"),
        ]

    def router(self):
        from .routes import registry_router

        return registry_router(self)
