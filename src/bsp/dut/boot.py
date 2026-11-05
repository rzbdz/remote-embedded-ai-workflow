"""Boot executor: runs an SDK boot recipe (load | download) against one DUT."""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from ..core.errors import HardwareError, HardwareTimeout, InvalidRequest
from ..sdk.catalog import Instance
from .control import ControlBackend
from .hostio import ArtifactStore, Fastboot, SerialConsole

Log = Callable[[str], None]


class BootRunner:
    def __init__(self, dut_id: str, control: ControlBackend, fastboot: Fastboot, console: SerialConsole,
                 store: ArtifactStore, hooks=None, download_mode: dict | None = None):
        self.dut_id = dut_id
        self.control = control
        self.fb = fastboot
        self.console = console
        self.store = store
        self.hooks = hooks
        # what download (FDL) mode looks like: {console: regex, fastboot: regex}; empty = any fastboot device
        self.download_mode = download_mode or {}

    # ------------------------------------------------------------ waits --
    def wait_fastboot(self, product: str, timeout: float, log: Log) -> dict[str, str]:
        rx = re.compile(product)
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.fb.state()
            if last and rx.search(last["product"]):
                return last
            time.sleep(0.3)
        raise HardwareTimeout(
            f"fastboot /{product}/ did not appear on {self.fb.usb} within {timeout:g}s (last: {last})",
            hint="check USB cabling and `bsp dut status`; the console tail is in details",
            details={"console_tail": "\n".join(self.console.tail(15))},
        )

    def download_evidence(self, mark) -> "Callable[[], str | None]":
        """A check that reports how the board shows it is in download mode (console or USB)."""
        con_rx = re.compile(self.download_mode["console"]) if self.download_mode.get("console") else None
        fb_rx = re.compile(self.download_mode.get("fastboot") or ".")

        def seen() -> str | None:
            if con_rx and con_rx.search(self.console.read_since(mark)):
                return f"download mode on the console (/{con_rx.pattern}/)"
            try:
                st = self.fb.state()
            except Exception:  # noqa: BLE001 - no fastboot yet is just "not seen"
                st = None
            if st and fb_rx.search(st.get("product", "")):
                return f"download-mode device on {self.fb.usb} ({st['product']})"
            return None
        return seen

    def enter_fdl(self, log: Log, timeout: float = 30) -> None:
        mark = self.console.mark()
        seen = self.download_evidence(mark)
        self.control.enter_fdl(log, seen)
        # Either sign will do: the boot ROM's banner on the console, or its download device on USB. A board that was
        # already in download mode prints no new banner, so waiting for the console alone failed on it.
        deadline = time.monotonic() + timeout
        while not seen():
            if time.monotonic() > deadline:
                raise HardwareTimeout(
                    f"no sign of download mode within {timeout:g}s: neither the boot ROM's banner on the console"
                    + (f" (/{self.download_mode['console']}/)" if self.download_mode.get("console") else "")
                    + f" nor its download device on {self.fb.usb}",
                    hint="see `bsp dut log <dut>`; the board may be in another stage",
                    details={"console_tail": "\n".join(self.console.tail(15))})
            time.sleep(0.3)
        dev = self.wait_fastboot(self.download_mode.get("fastboot") or ".", timeout, log)
        log(f"board in download mode on {dev['usb']} ({dev['product']})")

    # ------------------------------------------------------- file boot --
    def run_files(self, files: list[dict[str, Any]], log: Log, reset: bool = True, first_wait: float = 8.0,
                  later_wait: float = 3.0, port_timeout: float = 20.0, stage_timeout: float = 300.0,
                  poll: float = 0.5) -> dict[str, Any]:
        """Boot from RAM with files given in order (e.g. FSBL, U-Boot/OpenSBI, kernel FIT): for each one
        `stage` + `continue`, then wait for the download port to come back before the next. The first
        wait is longer (the boot ROM hands over to FSBL, which re-enumerates). Never flashes."""
        t0 = time.monotonic()
        log(f"boot {self.dut_id} from {len(files)} file(s)" + ("" if reset else " (board already in download mode)"))
        for i, f in enumerate(files, 1):
            log(f"file {i}/{len(files)}: {f['name']} ({f['size']:,} B, sha256 {f['sha256']})")
        if reset:
            self.enter_fdl(log)
        elif not self.fb.state():
            raise HardwareError(f"{self.dut_id}: no download port on {self.fb.usb}",
                                hint=f"put it in download mode first (`bsp dut reset {self.dut_id}`), or drop --no-reset")
        for i, f in enumerate(files, 1):
            label = f"file {i}/{len(files)} {f['name']}"
            if i > 1:
                settle = first_wait if i == 2 else later_wait
                time.sleep(settle)
                deadline = time.monotonic() + port_timeout
                while not self.fb.state():
                    if time.monotonic() > deadline:
                        raise HardwareTimeout(
                            f"download port {self.fb.usb} did not come back within {settle + port_timeout:g}s "
                            f"after file {i - 1}; it may not have started",
                            hint=f"see `bsp dut log {self.dut_id}`", details={"console_tail": "\n".join(self.console.tail(15))})
                    time.sleep(poll)
            ts = time.monotonic()
            self.fb.cmd("stage", f["path"], timeout=stage_timeout)
            try:
                self.fb.cmd("continue", timeout=30)
            except HardwareError:
                pass  # the responder detaches on purpose; the next wait is the real check
            log(f"{label}: staged and continued in {time.monotonic() - ts:.1f}s")
        log(f"done in {time.monotonic() - t0:.1f}s")
        return {"dut": self.dut_id, "files": [{k: f[k] for k in ("name", "sha256", "size")} for f in files],
                "elapsed_s": round(time.monotonic() - t0, 1)}

    # ------------------------------------------------------------- run --
    def run(self, sdk: Instance, method: str, variant: str | None, log: Log) -> dict[str, Any]:
        plan = sdk.boot_plan(method, variant)
        roles = sorted({s["artifact"] for s in plan["steps"] if "artifact" in s}
                       | {r for s in plan["steps"] for r in s.get("artifacts", [])})
        log(f"boot {self.dut_id} with {sdk.id} [{method}{'/' + variant if variant else ''}]: {len(plan['steps'])} steps")
        arts = self.store.pull(sdk.server.ssh, {r: sdk.artifacts()[r] for r in roles}, log)
        mark = self.console.mark()
        t0 = time.monotonic()
        fdl = ((self.control.cfg if hasattr(self.control, "cfg") else {}).get("fdl") or {}).get("strategy", "strapped")
        for i, step in enumerate(plan["steps"], 1):
            kind = step["do"]
            label = f"step {i}/{len(plan['steps'])} {kind}"
            when = step.get("when")
            if when == "fdl-not-strapped" and fdl == "strapped":
                log(f"{label}: skipped — FDL is strapped on this board, it cannot cold-boot from media "
                    f"({step.get('why', 'media boot verification')})")
                continue
            if kind == "enter-fdl":
                self.enter_fdl(log, float(step.get("timeout", 30)))
                mark = self.console.mark()
            elif kind == "reset":
                mark = self.console.mark()
                self.control.reset(log)
            elif kind == "stage":
                a = arts[step["artifact"]]
                ts = time.monotonic()
                self.fb.cmd("stage", a["path"], timeout=float(step.get("timeout", 300)))
                log(f"{label}: staged {step['artifact']} ({a['size']:,} B) in {time.monotonic() - ts:.1f}s")
            elif kind == "continue":
                mark = self.console.mark()
                try:
                    self.fb.cmd("continue", timeout=30)
                except HardwareError:
                    pass  # the responder detaches on purpose; the next wait is the real check
                log(f"{label}")
            elif kind == "oem":
                self.fb.cmd("oem", step["cmd"], timeout=60)
                log(f"{label}: oem {step['cmd']} OKAY")
            elif kind == "getvar":
                val = self.fb.getvar(step["name"])
                if step.get("match") and not re.search(step["match"], val):
                    raise HardwareError(f"getvar {step['name']} = {val!r} does not match /{step['match']}/")
                log(f"{label}: {step['name']} = {val}")
            elif kind == "flash":
                a = arts[step["artifact"]]
                self.fb.cmd("flash", step["partition"], a["path"], timeout=float(step.get("timeout", 900)))
                log(f"{label}: flashed {step['partition']} <- {step['artifact']}")
            elif kind == "settle":
                log(f"{label}: {step['seconds']}s ({step.get('why', 'hardware timing')})")
                time.sleep(float(step["seconds"]))
            elif kind == "wait":
                timeout = float(step.get("timeout", 60))
                ts = time.monotonic()
                if step.get("serial"):
                    self.console.wait(mark, step["serial"], step.get("fail"), timeout, step.get("stall"),
                                      step.get("nudge"))
                if step.get("fastboot"):
                    self.wait_fastboot(step["fastboot"], max(5.0, timeout - (time.monotonic() - ts)), log)
                log(f"{label}: {step.get('serial') or step.get('fastboot')} after {time.monotonic() - ts:.1f}s")
            elif kind == "hook":
                if self.hooks is None:
                    raise InvalidRequest("hook steps need a hook runner on this host")
                req = {"dut": self.dut_id, "usb": self.fb.usb, "console": self.console.alias,
                       "fastboot": self.fb.binary, "args": step.get("args") or {},
                       "artifacts": {r: a["path"] for r, a in arts.items()}}
                res = self.hooks.run(step.get("kind", "flash"), step["name"], req,
                                     timeout=float(step.get("timeout", 1800)), log=log)
                brief = {k: v for k, v in res.data.items() if k != "flashed"}
                brief["partitions"] = [":".join(str(x) for x in (f.get("layout"), f.get("partition")) if x)
                                       if isinstance(f, dict) else str(f) for f in res.data.get("flashed", [])]
                log(f"{label}: {step.get('kind', 'flash')}/{step['name']} -> {brief}")
                # keep the console mark: the next wait can check what the bootloader printed while flashing
            else:
                raise InvalidRequest(f"unknown boot step {kind!r}")
        log(f"done in {time.monotonic() - t0:.1f}s")
        return {"dut": self.dut_id, "sdk": sdk.id, "method": method, "variant": variant,
                "artifacts": {r: {k: a[k] for k in ("remote", "sha256", "size", "age_s")} for r, a in arts.items()},
                "elapsed_s": round(time.monotonic() - t0, 1)}
