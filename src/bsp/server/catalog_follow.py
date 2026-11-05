"""Keep this host's copy of the registry's SDK catalog current.

The registry answers every announcement with its catalog revision and nudges hosts right after
a change. When the revision differs from the copy kept in <state>/catalog, the host fetches the
snapshot, writes it there in one step and publishes `catalog.updated`, and the modules reload
their catalogs. The copy outlives restarts and outages: without the registry the host keeps the
last catalog it had. Jobs already running keep the recipe they started with.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from pathlib import Path
from typing import Any

from ..registry.catalog import layer_rev, materialize

log = logging.getLogger(__name__)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CatalogFollower:
    def __init__(self, registry_url: str, layer: Path, bus):
        self.url = registry_url.rstrip("/") + "/api/v1/catalog/snapshot"
        self.layer, self.bus = Path(layer), bus
        self.rev = layer_rev(self.layer)
        self.last: dict[str, Any] = {}
        self._lock = threading.Lock()

    def sync(self) -> dict[str, Any]:
        with self._lock:
            try:
                with _OPENER.open(urllib.request.Request(self.url, headers={"Accept": "application/json"}), timeout=15) as r:
                    snap = json.loads(r.read())["result"]
            except Exception as e:  # noqa: BLE001 - keep the copy we have
                self.last = {"ok": False, "error": str(e), "rev": self.rev}
                log.warning("catalog: cannot fetch %s: %s (keeping revision %s)", self.url, e, self.rev)
                return self.last
            changed = snap["rev"] != self.rev
            if changed:
                materialize(snap, self.layer)
                old, self.rev = self.rev, snap["rev"]
                self.bus.publish("catalog.updated", rev=self.rev, previous=old, documents=len(snap["docs"]))
            self.last = {"ok": True, "rev": self.rev, "changed": changed}
            return self.last

    def check(self, remote_rev: Any) -> None:
        """The registry said its revision; fetch in the background when ours differs."""
        try:
            if int(remote_rev) != self.rev:
                threading.Thread(target=self.sync, name="catalog sync", daemon=True).start()
        except (TypeError, ValueError):
            pass
