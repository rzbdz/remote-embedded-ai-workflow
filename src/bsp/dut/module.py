"""dut module: the DUTs attached to this host — registry, leases, reset/FDL,
fastboot boot (load / download), serial console, and auto-discovery (probe).
Commands are the atomic layer under `bsp dut ...` / `bsp probe`."""

from __future__ import annotations

import os

from typing import Any

from ..core.cmdspec import CommandSpec, Param
from ..core.commands import Command
from ..core.errors import Conflict, InvalidRequest, BspError
from ..core.hooks import HookRunner
from ..core.module import MODULES, Module, ModuleContext
from ..sdk.catalog import BOOT_METHODS, SdkCatalog
from .boot import BootRunner
from .catalog import DutRegistry, DutSpec
from .control import make_control
from .hostio import ArtifactStore, Fastboot, SerialConsole
from .leases import LeaseManager

DUT = Param("dut", help="DUT id (see `bsp dut list`)")
LEASE = Param("lease", help="lease token from `bsp dut acquire` (the CLI fills it in / $BSP_LEASE)",
              required=False, positional=False)


class Board:
    """Runtime handles for one DUT (built from its spec)."""

    def __init__(self, spec: DutSpec, mod: "DutModule"):
        self.spec = spec
        self.control = make_control(spec, mod.ctx.service)
        self.fastboot = Fastboot(spec.usb, mod.fastboot_bin)
        self.console = SerialConsole(spec.console_alias, mod.smtty, mod.serial_log_dir)

    def runner(self, mod: "DutModule") -> BootRunner:
        expect = (self.spec.control.get("fdl") or {}).get("expect") or mod.socs.download_mode(self.spec.soc)
        return BootRunner(self.spec.id, self.control, self.fastboot, self.console, mod.store, mod.hooks, expect)


@MODULES.register("dut")
class DutModule(Module):
    name = "dut"
    title = "DUTs"

    def __init__(self, ctx: ModuleContext, config: dict[str, Any]):
        super().__init__(ctx, config)
        from ..core.site import layered

        base = ctx.config_dir
        site = (base / config.get("site", "..")).resolve()    # <site>/hosts/<id>.yaml -> <site>
        state = ctx.module_state_dir("dut")
        self.smtty = config.get("smtty", "smtty")
        self.serial_mux = config.get("serial_mux", "serial-mux")
        self.serial_log_dir = config.get("serial_log_dir", os.path.expanduser("~/.serial-mux/logs"))
        self.fastboot_bin = config.get("fastboot", "fastboot")
        self.probe_cfg = config.get("probe") or {}
        self.sdks = SdkCatalog(layered("sdk", site))
        from .socs import SocProfiles

        self.socs = SocProfiles(layered("socs", site))
        self.registry = DutRegistry(ctx.host_id, site / "duts",
                                    state / "duts.json")
        self.leases = LeaseManager(state / "leases.json")
        self.store = ArtifactStore(state / "artifacts")
        self.hooks = HookRunner(tuple(str((base / d).resolve()) for d in config.get("hook_dirs", []))
                                + tuple(str(d) for d in reversed(layered("hooks", site))))
        from .consoles import ConsoleManager

        self.consoles = ConsoleManager(self.serial_mux, self.serial_log_dir)
        self.boards: dict[str, Board] = {}
        self._busy: dict[str, str] = {}
        self._rebuild()

    def _rebuild(self) -> None:
        self.boards = {i: Board(s, self) for i, s in sorted(self.registry.all().items())}

    # ------------------------------------------------------------ helpers --
    def board(self, dut: str) -> Board:
        if dut in self.boards:
            return self.boards[dut]
        import difflib

        close = difflib.get_close_matches(dut, list(self.boards), n=3, cutoff=0.4)
        from ..core.errors import NotFound

        raise NotFound(f"no DUT {dut!r} on host {self.ctx.host_id}",
                       hint=(f"did you mean: {', '.join(close)}? " if close else "")
                       + f"DUTs here: {', '.join(self.boards) or '(none — run `bsp probe`)'}")

    def _event(self, topic: str, **data: Any) -> None:
        self.ctx.bus.publish(f"dut.{topic}", **data)

    def _guard_busy(self, dut: str) -> None:
        if dut in self._busy:
            raise Conflict(f"{dut} is busy: {self._busy[dut]}", hint="wait for the running job (`bsp job list`)")

    def _start_job(self, dut: str, what: str, body, **meta):
        self._busy[dut] = what

        def wrapped(log):
            try:
                return body(log)
            finally:
                self._busy.pop(dut, None)

        return self.ctx.jobs.start(meta.pop("kind", "dut"), f"{dut}: {what}", wrapped, dut=dut, **meta)

    # ----------------------------------------------------------- commands --
    def status(self, dut: str | None = None) -> dict[str, Any]:  # type: ignore[override]
        if dut is None:   # module health (hub status)
            return {"duts": sorted(self.boards), "hooks": {k: self.hooks.names(k) for k in ("probe", "flash")}}
        b = self.board(dut)
        try:
            fb = b.fastboot.state()
        except BspError as e:
            fb = {"error": e.message}
        try:
            ctl = b.control.describe()
        except BspError as e:
            ctl = {"error": e.message}
        try:
            tail = b.console.tail(4)
        except OSError:
            tail = []
        return {**b.spec.summary(), "sdks": self.effective_sdks(b.spec), "sdks_explicit": bool(b.spec.sdks),
                "origin": self.registry.origin(dut), "lease": self.leases.view(dut),
                "busy": self._busy.get(dut), "fastboot": fb,
                "stage": _stage(fb, self.socs.download_mode(b.spec.soc).get("fastboot")),
                "control_state": ctl, "console_tail": tail}

    def list(self) -> list[dict[str, Any]]:
        return [self.status(d) for d in self.boards]

    def acquire(self, dut: str, holder: str, ttl: int, purpose: str, lease: str | None) -> dict[str, Any]:
        self.board(dut)
        out = self.leases.acquire(dut, holder, ttl, purpose, lease)
        self._event("leased", dut=dut, holder=holder, purpose=purpose)
        return {"dut": dut, "host": self.ctx.host_id,
                **{k: out[k] for k in ("token", "holder", "purpose", "expires_in_s")}}

    def release(self, dut: str, lease: str | None, force: bool) -> dict[str, Any]:
        self.board(dut)
        out = self.leases.release(dut, lease, force)
        self._event("released", dut=dut, forced=force)
        return out

    def reset(self, dut: str, lease: str | None, fdl: bool) -> dict[str, Any]:
        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        lines: list[str] = []
        if fdl:
            b.runner(self).enter_fdl(lines.append)
        else:
            b.control.reset(lines.append)
        self._event("reset", dut=dut, fdl=fdl)
        return {"dut": dut, "fdl": fdl, "log": lines, "fastboot": b.fastboot.state()}

    def boot(self, dut: str, sdk: str, method: str, variant: str | None, lease: str | None) -> dict[str, Any]:
        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        inst = self.sdks.get(sdk)
        if inst.soc and b.spec.soc and inst.soc != b.spec.soc:
            raise InvalidRequest(f"sdk {sdk} is for {inst.soc}, but {dut} is a {b.spec.soc} board ({b.spec.board})",
                                 hint=f"SDKs for {b.spec.soc}: {', '.join(self.effective_sdks(b.spec)) or '(none)'}")
        if sdk not in self.effective_sdks(b.spec):
            raise InvalidRequest(f"{dut} ({b.spec.board}) is not declared to run sdk {sdk}",
                                 hint=f"supported: {', '.join(self.effective_sdks(b.spec))}; "
                                      f"change with `bsp dut set {dut} --sdks ...` (empty = all SDKs of its SoC)")
        inst.boot_plan(method, variant)       # validate first: todo methods fail here with their notes
        runner = b.runner(self)
        job = self._start_job(dut, f"boot {sdk} [{method}]", lambda log: runner.run(inst, method, variant, log),
                              kind="boot", sdk=sdk, method=method)
        self._event("boot", dut=dut, sdk=sdk, method=method, job=job.id)
        return {"job": job.id, "dut": dut, "sdk": sdk, "method": method, "host": self.ctx.host_id}

    def console(self, dut: str, cmd: str, expect: str | None, timeout: float, lease: str | None) -> dict[str, Any]:
        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        out = b.console.run(cmd, expect or self._prompt_for(b), timeout)
        self._event("console", dut=dut, cmd=cmd)
        return {"dut": dut, **out}

    def effective_sdks(self, spec: DutSpec) -> list[str]:
        """Declared sdks, else every SDK instance for the board's SoC."""
        if spec.sdks:
            return list(spec.sdks)
        return sorted(i for i, inst in self.sdks.instances.items() if spec.soc and inst.soc == spec.soc)

    def _prompt_for(self, b: Board) -> str:
        explicit = b.spec.console.get("prompt")
        if explicit:
            return explicit
        prompts = {self.sdks.get(s).prompt for s in self.effective_sdks(b.spec) if s in self.sdks.instances}
        return "|".join(sorted(prompts)) or "# $"

    def log(self, dut: str, lines: int, grep: str | None) -> dict[str, Any]:
        return {"dut": dut, "lines": self.board(dut).console.tail(lines, grep)}

    def add(self, spec: Any, replace: bool) -> dict[str, Any]:
        import json

        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except ValueError as e:
                raise InvalidRequest(f"spec is not valid JSON: {e}") from None
        if not isinstance(spec, dict):
            raise InvalidRequest("spec must be a JSON object (see examples/site/duts/*.yaml for the shape)")
        con = spec.get("console") or {}
        start = con.pop("start", False)
        s = self.registry.add(spec, replace)
        if start or con.get("device"):
            self.consoles.ensure(con["alias"], con.get("device"))
        self._rebuild()
        self.boards[s.id].control.setup()
        self._event("added", dut=s.id)
        return self.status(s.id)

    def _free_for_edit(self, dut: str, lease: str | None) -> None:
        cur = self.leases.view(dut)
        if cur:
            self.leases.check(dut, lease)        # only the holder may edit a leased DUT
        self._guard_busy(dut)

    def rename(self, dut: str, new: str, lease: str | None, console: bool) -> dict[str, Any]:
        import re

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", new or ""):
            raise InvalidRequest(f"invalid DUT name {new!r}", hint="letters, digits, _ . - (max 63)")
        self._free_for_edit(dut, lease)
        old = self.board(dut).spec
        raw = {**old.raw, "id": new}
        renamed_console = None
        if console or old.console_alias == old.id:
            raw["console"] = {**old.console, "alias": new}
            renamed_console = (old.console_alias, new)
        self.registry.update(dut, raw)
        self._drop_aliases(dut)
        if renamed_console:
            self.consoles.rename(*renamed_console, old.console.get("device"))
        lease_info = self.leases.view(dut)
        if lease_info:
            self.leases.release(dut, lease, force=True)
        self._rebuild()
        self.boards[new].control.setup()
        self._event("renamed", dut=new, old=dut)
        return {**self.status(new), "renamed_from": dut,
                "note": "lease dropped by rename; acquire the new name" if lease_info else None}

    def set_fields(self, dut: str, lease: str | None, **fields: Any) -> dict[str, Any]:
        self._free_for_edit(dut, lease)
        spec = self.board(dut).spec
        raw = {**spec.raw, "console": dict(spec.console), "fastboot": dict(spec.fastboot),
               "control": {**spec.control, "reset": dict(spec.control.get("reset") or {}),
                           "fdl": dict(spec.control.get("fdl") or {})}}
        changed = []
        for key in ("board", "soc", "description"):
            if fields.get(key) is not None:
                raw[key] = fields[key]
                changed.append(key)
        if fields.get("sdks") is not None:
            raw["sdks"] = [x for x in fields["sdks"].split(",") if x.strip()]
            unknown = [x for x in raw["sdks"] if x not in self.sdks.instances]
            if unknown:
                raise InvalidRequest(f"unknown sdk(s) {unknown}", hint=f"known: {', '.join(self.sdks.instances.names())}")
            changed.append("sdks")
        if fields.get("usb"):
            raw["fastboot"]["usb"] = fields["usb"]
            changed.append("usb")
        if fields.get("reset_pin"):
            raw["control"]["reset"]["pin"] = fields["reset_pin"]
            changed.append("reset_pin")
        if fields.get("pulse_ms"):
            raw["control"]["reset"]["pulse_ms"] = int(fields["pulse_ms"])
            changed.append("pulse_ms")
        if fields.get("fdl"):
            raw["control"]["fdl"]["strategy"] = fields["fdl"]
            changed.append("fdl")
        if fields.get("fdl_pin"):
            raw["control"]["fdl"]["pin"] = fields["fdl_pin"]
            changed.append("fdl_pin")
        if not changed:
            raise InvalidRequest("nothing to change", hint="see `bsp dut set --help`")
        self.registry.update(dut, raw)
        self._drop_aliases(dut)
        self._rebuild()
        self.boards[dut].control.setup()
        self._event("updated", dut=dut, fields=changed)
        return {**self.status(dut), "changed": changed}

    def export(self) -> dict[str, Any]:
        keep = ("id", "host", "board", "soc", "description", "console", "fastboot", "control", "sdks", "identity")
        return {"duts": [{k: s.raw[k] for k in keep if k in s.raw} for s in self.registry.all().values()]}

    def _drop_aliases(self, dut: str) -> None:
        try:
            self.ctx.service("gpio_relay").alias_delete(dut, prefix=True)
        except BspError:
            pass

    def remove(self, dut: str, force: bool) -> dict[str, Any]:
        if self.leases.view(dut) and not force:
            raise Conflict(f"{dut} is leased", hint="release it first or pass force=true")
        s = self.registry.remove(dut)
        self._drop_aliases(dut)
        self._rebuild()
        self._event("removed", dut=dut)
        return {"removed": s.id}

    def probe(self, pins: str | None, consoles: str | None, identify: bool, sdks: str | None,
              dry_run: bool) -> dict[str, Any]:
        from .probe import Prober

        prober = Prober(self, pins=pins, consoles=consoles, identify=identify,
                        default_sdks=[s for s in (sdks or "").split(",") if s] or None)
        if dry_run:
            return prober.plan()
        job = self.ctx.jobs.start("probe", f"{self.ctx.host_id}: probe", prober.run, host=self.ctx.host_id)
        return {"job": job.id, "host": self.ctx.host_id}

    # ----------------------------------------------------------- plumbing --
    def startup(self) -> None:
        for b in self.boards.values():
            try:
                b.control.setup()
            except BspError as e:
                self._event("error", dut=b.spec.id, error=e.to_dict())
            try:
                self.consoles.ensure(b.spec.console_alias, b.spec.console.get("device"), only_if_known=True)
            except BspError as e:
                self._event("error", dut=b.spec.id, error=e.to_dict())

    def commands(self) -> list[Command]:
        def cmd(name, summary, handler, params=(), render=None, mutating=True, description=""):
            return Command(CommandSpec(name, summary, list(params), description, mutating), handler, render)

        return [
            cmd("dut.list", "DUTs on this host: stage, lease, USB path, console", self.list,
                render=_render_list, mutating=False),
            cmd("dut.status", "One DUT: lease, stage (fdl/fastboot/os), fastboot, control lines, console tail",
                self.status, [DUT], render=_render_status, mutating=False),
            cmd("dut.acquire", "Lease a DUT (needed for reset/boot/console); returns a token",
                self.acquire,
                [DUT, Param("holder", help="who holds it (user or agent id)", required=False, positional=False,
                            default="anonymous"),
                 Param("ttl", "integer", "lease seconds (max 86400)", required=False, default=3600),
                 Param("purpose", help="free text shown to others", required=False, default=""), LEASE],
                render=lambda r: f"leased {r['dut']} on {r['host']} to {r['holder']} for {r['expires_in_s']}s\n"
                                 f"token: {r['token']}",
                description="Re-acquiring with your own token renews the lease."),
            cmd("dut.release", "Release a DUT lease", self.release,
                [DUT, LEASE, Param("force", "boolean", "operator override", required=False, default=False)],
                render=lambda r: f"{r['resource']}: {'released' if r['released'] else r.get('message')}"),
            cmd("dut.reset", "Reset the DUT via its control backend; by default waits until it is in FDL",
                self.reset,
                [DUT, LEASE, Param("fdl", "boolean", "enter/expect FDL download mode", required=False, default=True)],
                render=lambda r: "\n".join(r["log"]) + f"\nfastboot: {r['fastboot']}"),
            cmd("dut.boot", "Boot an SDK: load = fastboot RAM boot, download = flash to media",
                self.boot,
                [DUT, Param("sdk", help="sdk instance (bsp sdk list)", positional=False),
                 Param("method", help="load | download", required=False, default="load", choices=list(BOOT_METHODS)),
                 Param("variant", help="e.g. uboot-only", required=False), LEASE],
                render=lambda r: f"job {r['job']}: {r['dut']} boot {r['sdk']} [{r['method']}]",
                description="Pulls the SDK's artifacts from its build server into the host cache, then runs the "
                            "recipe (enter-fdl, stage, continue, waits on serial + fastboot). Background job: the "
                            "CLI follows it and exits non-zero if it fails."),
            cmd("dut.console", "Run one shell command on the DUT's serial console, return its output",
                self.console,
                [DUT, Param("cmd", help="command line, e.g. 'dmesg | grep foo'"),
                 Param("expect", help="regex marking completion (default: the SDK shell prompt)", required=False),
                 Param("timeout", "number", "seconds", required=False, default=20), LEASE],
                render=lambda r: r["output"]),
            cmd("dut.log", "Recent console lines (passive: no keystrokes, no lease)", self.log,
                [DUT, Param("lines", "integer", "how many", required=False, default=40),
                 Param("grep", help="regex filter", required=False)],
                render=lambda r: "\n".join(r["lines"]), mutating=False),
            cmd("dut.add", "Register a DUT on this host from a JSON spec (what probe writes)", self.add,
                [Param("spec", "string", "JSON object: id, board, console{alias}, fastboot{usb}, control{...}, sdks"),
                 Param("replace", "boolean", "overwrite an existing id", required=False, default=False)],
                render=_render_status),
            cmd("dut.remove", "Forget a probed/added DUT", self.remove,
                [DUT, Param("force", "boolean", "even if leased", required=False, default=False)]),
            cmd("dut.rename", "Rename a DUT (aliases follow; console alias too if it was the DUT name)",
                self.rename,
                [DUT, Param("new", help="new DUT name"), LEASE,
                 Param("console", "boolean", "also rename the serial-mux console alias", required=False, default=False)],
                render=_render_status),
            cmd("dut.set", "Edit a DUT: board, soc, description, sdks, usb, reset pin, FDL strategy",
                lambda dut, lease, **f: self.set_fields(dut, lease, **f),
                [DUT, LEASE,
                 Param("board", help="board name", required=False, positional=False),
                 Param("soc", help="SoC id (see the SoC profiles)", required=False, positional=False),
                 Param("description", help="free text", required=False, positional=False),
                 Param("sdks", help="comma list; empty string = all SDKs of its SoC", required=False, positional=False),
                 Param("usb", help="fastboot selector, e.g. usb:2-1.4", required=False, positional=False),
                 Param("reset_pin", help="header pin of the reset line, e.g. P26", required=False, positional=False),
                 Param("pulse_ms", "integer", "reset pulse width", required=False, positional=False),
                 Param("fdl", help="strapped | button", required=False, positional=False, choices=["strapped", "button"]),
                 Param("fdl_pin", help="header pin of the FDL line (fdl=button)", required=False, positional=False)],
                render=_render_status),
            cmd("dut.export", "All DUT specs on this host (save into <site>/duts/ to make them static)",
                self.export, mutating=False,
                render=lambda r: __import__("yaml").safe_dump(r, sort_keys=False, allow_unicode=True)),
            cmd("dut.probe", "Discover boards on this host: pulse free GPIOs, watch consoles + USB, identify",
                self.probe,
                [Param("pins", help="comma list of candidate reset pins (default: all free header GPIOs)", required=False),
                 Param("consoles", help="comma list of console candidates (serial-mux alias or /dev/tty*)", required=False),
                 Param("identify", "boolean", "boot U-Boot to read board identity (TLV)", required=False, default=True),
                 Param("sdks", help="comma list of sdks the found boards run (default by soc)", required=False),
                 Param("dry_run", "boolean", "only show candidates, touch nothing", required=False, default=False)],
                render=_render_probe,
                description="Hook-driven (hooks/probe/*). Results are proposals: confirm with `bsp dut add`."),
        ]


# =================================================================== views --
def _stage(fb: dict | None, download_rx: str | None) -> str:
    """Coarse stage from the fastboot view only: download mode / fastboot / not on the bus."""
    import re

    if not fb or fb.get("error"):
        return "off-bus"
    if download_rx and re.search(download_rx, fb.get("product", "")):
        return "download-mode"
    return "fastboot"


def _render_list(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(no DUTs on this host — run `bsp probe`)"
    rows = [("DUT", "HOST", "BOARD", "STAGE", "LEASE", "USB", "CONSOLE")]
    for d in items:
        lease = d["lease"]
        rows.append((d["id"], d["host"], d["board"], d["stage"] + (f" [{d['busy']}]" if d.get("busy") else ""),
                     f"{lease['holder']} ({lease['expires_in_s']}s)" if lease else "free", d["usb"], d["console"]))
    w = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in rows)


def _render_status(d: dict[str, Any]) -> str:
    lease = d["lease"]
    return "\n".join([
        f"{d['id']} @ {d['host']}: {d['board']} ({d['soc']}) — {d['description']} [{d.get('origin')}]",
        f"  stage    : {d['stage']}" + (f"  busy: {d['busy']}" if d.get("busy") else ""),
        (f"  lease    : {lease['holder']} ({lease['expires_in_s']}s left, {lease.get('purpose') or '-'})"
         if lease else "  lease    : free"),
        f"  fastboot : {d['fastboot'] or 'not on bus'} (usb {d['usb']})",
        f"  control  : {d['control_state']}",
        f"  sdks     : {', '.join(d['sdks'])}",
        "  console  :", *[f"    {l}" for l in d["console_tail"]],
    ])


def _render_probe(r: dict[str, Any]) -> str:
    if "job" in r:
        return f"probe job {r['job']} on {r['host']}"
    import json

    return json.dumps(r, indent=2, ensure_ascii=False)
