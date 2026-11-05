"""SoC profiles (<layer>/socs/<soc>/soc.yaml, merged across layers: packs then site):
everything bsp knows about a SoC is data.

    soc: <id>
    fingerprint: [regex, ...]          boot-ROM console lines after a reset (probe: best match wins)
    download_mode: {console: regex, fastboot: regex}   what "FDL / download mode" looks like
    usb_vids: [hex, ...]               USB vendor ids of its download / fastboot gadgets
    images: {key: {file, sha256}}      probe images; each resolves to the highest layer holding the file
    identify: {sequence, fields, placeholders, name}   see hooks/probe/identify
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class SocProfiles:
    def __init__(self, directories: "Path | list[Path] | None"):
        dirs = [Path(d) for d in (directories if isinstance(directories, (list, tuple)) else
                                  [directories] if directories else [])]
        self.profiles: dict[str, dict[str, Any]] = {}
        self.dirs: dict[str, list[Path]] = {}
        import yaml

        for d in dirs:                                   # lowest layer first
            for p in sorted(Path(d).glob("*/soc.yaml")) if Path(d).is_dir() else []:
                data = yaml.safe_load(p.read_text()) or {}
                soc = data.get("soc") or p.parent.name
                self.profiles[soc] = _merge(self.profiles.get(soc, {"soc": soc}), data)
                self.dirs.setdefault(soc, []).append(p.parent)
        for soc, prof in self.profiles.items():        # resolve image files, highest layer wins
            for img in (prof.get("images") or {}).values():
                for d in reversed(self.dirs[soc]):
                    if (d / img.get("file", "")).is_file():
                        img["path"] = str(d / img["file"])
                        break

    def get(self, soc: str | None) -> dict[str, Any]:
        return self.profiles.get(soc or "", {})

    def detect(self, text: str) -> tuple[str | None, int]:
        """SoC whose boot-ROM fingerprint matches `text` best (None when nothing matches)."""
        best, best_n = None, 0
        for soc, p in self.profiles.items():
            n = sum(1 for rx in p.get("fingerprint", []) if re.search(rx, text))
            if n > best_n:
                best, best_n = soc, n
        return best, best_n

    def download_mode(self, soc: str | None) -> dict[str, str]:
        return dict(self.get(soc).get("download_mode") or {})

    def in_download_mode(self, console_text: str, fastboot_product: str) -> bool:
        """Does any profile recognise this console text / fastboot product as download mode?"""
        for p in self.profiles.values():
            dm = p.get("download_mode") or {}
            if dm.get("console") and re.search(dm["console"], console_text):
                return True
            if dm.get("fastboot") and re.search(dm["fastboot"], fastboot_product or ""):
                return True
        return False

    def usb_vids(self) -> list[str]:
        return sorted({v.lower() for p in self.profiles.values() for v in p.get("usb_vids", [])})
