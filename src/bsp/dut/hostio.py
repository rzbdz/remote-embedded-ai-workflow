"""Host-side adapters: fastboot CLI, serial-mux (smtty + its log), artifact pulls."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

from ..core.errors import Conflict, HardwareError, HardwareTimeout, InvalidRequest

Log = Callable[[str], None]


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HardwareTimeout(f"`{' '.join(argv[:4])} ...` timed out after {timeout:g}s") from None
    except FileNotFoundError:
        raise HardwareError(f"{argv[0]} not found on this host", hint="install it or set its path in the host config") from None


# ------------------------------------------------------------- fastboot --
class Fastboot:
    """fastboot CLI bound to one USB path (multi-DUT safe: always `-s usb:<path>`)."""

    def __init__(self, usb: str, binary: str = "fastboot"):
        self.usb = usb
        self.binary = binary

    def devices(self) -> list[dict[str, str]]:
        r = _run([self.binary, "devices", "-l"], 10)
        out, entries, cur = (r.stdout + r.stderr).splitlines(), [], None
        for line in out:
            if not line.strip():
                continue
            if line.startswith(" ") and cur is not None and "usb:" in line:
                cur["usb"] = line.strip().split()[0]
                continue
            parts = line.split(None, 1)
            cur = {"serial": parts[0], "product": parts[1].strip() if len(parts) > 1 else "", "usb": ""}
            m = re.search(r"(usb:\S+)", line)
            if m:
                cur["usb"] = m.group(1)
            entries.append(cur)
        return entries

    def state(self) -> dict[str, str] | None:
        """This DUT's fastboot entry, or None if it is not on the bus."""
        for d in self.devices():
            if d["usb"] == self.usb:
                return d
        return None

    def cmd(self, *args: str, timeout: float = 120) -> str:
        r = _run([self.binary, "-s", self.usb, *args], timeout)
        out = (r.stdout + r.stderr).strip()   # fastboot reports on stderr
        if r.returncode != 0 or "FAILED" in out:
            raise HardwareError(f"fastboot {' '.join(args)} failed: {out[-300:]}",
                                hint="check `bsp dut status` (is the board in the expected stage?)")
        return out

    def getvar(self, name: str) -> str:
        out = self.cmd("getvar", name, timeout=15)
        m = re.search(rf"^{re.escape(name)}:\s*(.*)$", out, re.M)
        return m.group(1).strip() if m else out


# --------------------------------------------------------------- serial --
class SerialConsole:
    """serial-mux alias: commands via `smtty --send/--wait`, passive reads from its
    per-day log (cumulative, append-only) without injecting keystrokes."""

    def __init__(self, alias: str, smtty: str = "smtty", log_dir: str = "~/.serial-mux/logs"):
        self.alias = alias
        self.smtty = smtty
        self.log_dir = Path(log_dir).expanduser() / alias

    def _log_path(self, day: date | None = None) -> Path:
        return self.log_dir / f"{(day or date.today()).isoformat()}.log"

    def mark(self) -> tuple[Path, int]:
        p = self._log_path()
        return p, (p.stat().st_size if p.exists() else 0)

    def read_since(self, mark: tuple[Path, int]) -> str:
        path, off = mark
        chunks = []
        if path.exists():
            with open(path, "rb") as f:
                f.seek(off)
                chunks.append(f.read())
        today = self._log_path()
        if today != path and today.exists():          # day rollover
            chunks.append(today.read_bytes())
        return b"".join(chunks).decode("utf-8", errors="replace")

    def tail(self, lines: int = 50, grep: str | None = None) -> list[str]:
        files = sorted(self.log_dir.glob("*.log"))
        out: list[str] = []
        for p in reversed(files[-2:]):
            out = p.read_bytes().decode("utf-8", errors="replace").splitlines() + out
            if len(out) >= lines * 4:
                break
        if grep:
            rx = re.compile(grep)
            out = [l for l in out if rx.search(l)]
        return out[-lines:]

    def nudge(self) -> None:
        """Press Enter on the console (best effort). The log is line-based, so a prompt
        printed without a newline (`~ # `) only shows up once something follows it."""
        try:
            _run([self.smtty, self.alias, "--send", " "], timeout=10)
        except Exception:  # noqa: BLE001 - a missed nudge only delays the wait
            pass

    def wait(self, mark: tuple[Path, int], pattern: str | None, fail: str | None,
             timeout: float, stall: float | None = None, nudge: float | None = None) -> str:
        """Wait for `pattern` in the log after `mark`. Raises on `fail` match or timeout.
        `nudge`: press Enter whenever the console has been quiet that many seconds."""
        rx = re.compile(pattern) if pattern else None
        frx = re.compile(fail) if fail else None
        deadline = time.monotonic() + timeout
        last_size, last_move = -1, time.monotonic()
        last_nudge = last_move
        while True:
            text = self.read_since(mark)
            if frx and frx.search(text):
                m = frx.search(text)
                ctx = text[max(0, m.start() - 400): m.end() + 200]
                raise HardwareError(f"console reported failure /{fail}/", details={"console": ctx[-800:]})
            if rx is None or rx.search(text):
                return text
            if len(text) != last_size:
                last_size, last_move = len(text), time.monotonic()
            now = time.monotonic()
            if nudge and now - max(last_move, last_nudge) > nudge:
                self.nudge()
                last_nudge = time.monotonic()
            if stall and now - last_move > stall:
                raise HardwareTimeout(f"console went silent for {stall:g}s while waiting for /{pattern}/",
                                      details={"console_tail": text[-800:]})
            if now > deadline:
                raise HardwareTimeout(f"/{pattern}/ not seen on console within {timeout:g}s",
                                      hint="see `bsp dut log <dut>`; the board may be in another stage",
                                      details={"console_tail": text[-800:]})
            time.sleep(0.25)

    def run(self, command: str, expect: str, timeout: float) -> dict[str, Any]:
        """Send one line, wait for `expect`, return the console text it produced."""
        if not command.strip():
            raise InvalidRequest("empty console command", hint="pass a shell command such as `dmesg | tail`")
        mark = self.mark()
        r = _run([self.smtty, self.alias, "--send", command, "--wait", expect, "--timeout", str(int(timeout))],
                 timeout + 15)
        text = self.read_since(mark)
        if r.returncode == 2:
            raise HardwareTimeout(f"prompt /{expect}/ not seen within {timeout:g}s after `{command}`",
                                  hint="the board may not be booted to a shell: `bsp dut status`",
                                  details={"output": (r.stdout or text)[-2000:]})
        if r.returncode not in (0,):
            raise HardwareError(f"smtty failed (rc={r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}",
                                hint=f"is serial-mux alias '{self.alias}' running? `serial-mux list` on the host")
        return {"command": command, "output": _strip_console(r.stdout, command), "raw": r.stdout[-8000:]}


_TS = re.compile(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ?")


def _strip_console(out: str, command: str) -> str:
    lines = [_TS.sub("", l).rstrip("\r") for l in out.splitlines()]
    # drop the echoed command line and the trailing prompt
    if lines and command.strip() and command.strip() in lines[0]:
        lines = lines[1:]
    while lines and re.fullmatch(r"\s*(~ )?#\s*", lines[-1]):
        lines.pop()
    return "\n".join(lines)


# ------------------------------------------------------------ artifacts --
class ArtifactStore:
    """Content-addressed cache of build outputs pulled from build servers over SSH.
    Boot always reads local files (never CIFS)."""

    SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def pull(self, ssh: str, files: dict[str, str], log: Log) -> dict[str, dict[str, Any]]:
        """files: role -> absolute remote path. Returns role -> {path, sha256, size, mtime}."""
        globs = {r: p for r, p in files.items() if any(c in p for c in "*?[")}
        if globs:
            script = "; ".join(f"echo {shlex.quote(r)} $(ls -t {p} 2>/dev/null | head -1)" for r, p in globs.items())
            rg = _run(["ssh", *self.SSH_OPTS, ssh, script], 60)
            for line in rg.stdout.splitlines():
                role, _, found = line.partition(" ")
                if found.strip():
                    files = {**files, role: found.strip()}
        paths = list(files.values())
        script = "for f in " + " ".join(shlex.quote(p) for p in paths) + \
                 '; do if [ -f "$f" ]; then echo "$(sha256sum "$f" | cut -d" " -f1) $(stat -c "%s %Y" "$f") $f"; else echo "MISSING - - $f"; fi; done'
        r = _run(["ssh", *self.SSH_OPTS, ssh, script], 120)
        if r.returncode != 0:
            raise HardwareError(f"cannot reach build server {ssh}: {r.stderr.strip()[-300:]}",
                                hint="this host needs an SSH key accepted by the build server")
        meta = {}
        for line in r.stdout.splitlines():
            sha, size, mtime, path = line.split(" ", 3)
            meta[path] = (sha, size, mtime)
        out: dict[str, dict[str, Any]] = {}
        for role, path in files.items():
            sha, size, mtime = meta.get(path, ("MISSING", "-", "-"))
            if sha == "MISSING":
                raise InvalidRequest(f"artifact '{role}' missing on {ssh}: {path}",
                                     hint="build it first: `bsp sdk build <sdk> <component>`")
            local = self.root / sha / Path(path).name
            if local.exists():
                log(f"artifact {role}: {Path(path).name} cached ({int(size):,} B, sha {sha[:12]})")
            else:
                local.parent.mkdir(parents=True, exist_ok=True)
                tmp = local.with_suffix(local.suffix + ".part")
                t0 = time.monotonic()
                rr = _run(["scp", "-q", *self.SSH_OPTS, f"{ssh}:{path}", str(tmp)], 600)
                if rr.returncode != 0:
                    raise HardwareError(f"scp {path} failed: {rr.stderr.strip()[-300:]}")
                got = _sha256(tmp)
                if got != sha:
                    tmp.unlink(missing_ok=True)
                    raise HardwareError(f"checksum mismatch pulling {path} (changed during copy?)",
                                        hint="retry once the build has finished writing it")
                os.replace(tmp, local)
                log(f"artifact {role}: pulled {Path(path).name} ({int(size):,} B in {time.monotonic() - t0:.1f}s, sha {sha[:12]})")
            out[role] = {"path": str(local), "remote": path, "sha256": sha, "size": int(size),
                         "mtime": int(mtime), "age_s": int(time.time()) - int(mtime)}
        return out


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure(cond: bool, msg: str, **kw: Any) -> None:
    if not cond:
        raise Conflict(msg, **kw)
