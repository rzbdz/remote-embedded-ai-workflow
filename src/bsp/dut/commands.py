"""dut command set: the single definition behind REST, CLI, console and AI tools for the DUTs of a
host. Each command is one atomic step of the DUT manager (`DutModule`), which composes the host's
relay controller (control backends over gpio_relay, or a person), its download path (fastboot and
the artifact cache) and its serial consoles (serial-mux) per board."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.cmdspec import CommandSpec, Param
from ..core.commands import Command
from ..sdk.catalog import BOOT_METHODS

if TYPE_CHECKING:
    from .module import DutModule

DUT = Param("dut", help="DUT id (see `bsp dut list`)")
LEASE = Param("lease", help="lease token from `bsp dut acquire` (the CLI fills it in / $BSP_LEASE)",
              required=False, positional=False)


def build_commands(mod: "DutModule") -> list[Command]:
    def cmd(name, summary, handler, params=(), render=None, mutating=True, description=""):
        return Command(CommandSpec(name, summary, list(params), description, mutating), handler, render)

    return [
        cmd("dut.list", "DUTs on this host: stage, lease, USB path, console", mod.list,
            render=_render_list, mutating=False),
        cmd("dut.status", "One DUT: lease, stage (fdl/fastboot/os), fastboot, control lines, console tail",
            mod.status, [DUT], render=_render_status, mutating=False),
        cmd("dut.acquire", "Lease a DUT (needed for reset/boot/console); returns a token",
            mod.acquire,
            [DUT, Param("holder", help="who holds it (user or agent id)", required=False, positional=False,
                        default="anonymous"),
             Param("ttl", "integer", "lease seconds (max 86400)", required=False, default=3600),
             Param("purpose", help="free text shown to others", required=False, default=""), LEASE],
            render=lambda r: f"leased {r['dut']} on {r['host']} to {r['holder']} for {r['expires_in_s']}s\n"
                             f"token: {r['token']}",
            description="Re-acquiring with your own token renews the lease."),
        cmd("dut.release", "Release a DUT lease", mod.release,
            [DUT, LEASE, Param("force", "boolean", "operator override", required=False, default=False)],
            render=lambda r: f"{r['resource']}: {'released' if r['released'] else r.get('message')}"),
        cmd("dut.reset", "Reset the DUT via its control backend; by default waits until it is in FDL",
            mod.reset,
            [DUT, LEASE, Param("fdl", "boolean", "enter/expect FDL download mode", required=False, default=True)],
            render=lambda r: (f"{r['dut']}: waiting for a person, job {r['job']}" if r.get("job")
                              else "\n".join(r["log"]) + f"\nfastboot: {r['fastboot']}")),
        cmd("dut.confirm", "Tell a waiting reset/boot that the requested manual action is done "
            "(human control backend)", mod.confirm,
            [DUT, Param("note", help="what you did, for the log", required=False, default="")],
            render=lambda r: f"{r['dut']}: confirmed '{r['confirmed']}' after {r['waited_s']}s"),
        cmd("dut.boot", "Boot an SDK: load = fastboot RAM boot, download = flash to media",
            mod.boot,
            [DUT, Param("sdk", help="sdk instance (bsp sdk list)", positional=False),
             Param("method", help="load | download", required=False, default="load", choices=list(BOOT_METHODS)),
             Param("variant", help="e.g. uboot-only", required=False), LEASE],
            render=lambda r: f"job {r['job']}: {r['dut']} boot {r['sdk']} [{r['method']}]",
            description="Pulls the SDK's artifacts from its build server into the host cache, then runs the "
                        "recipe (enter-fdl, stage, continue, waits on serial + fastboot). Background job: the "
                        "CLI follows it and exits non-zero if it fails."),
        cmd("dut.console", "Run one shell command on the DUT's serial console, return its output",
            mod.console,
            [DUT, Param("cmd", help="command line, e.g. 'dmesg | grep foo'"),
             Param("expect", help="regex marking completion (default: the SDK shell prompt)", required=False),
             Param("timeout", "number", "seconds", required=False, default=20), LEASE],
            render=lambda r: r["output"]),
        cmd("dut.renew", "Extend your lease on a DUT (holder, purpose and token stay the same)", mod.renew,
            [DUT, LEASE, Param("ttl", "integer", "new lease seconds from now (max 86400)", required=False,
                               default=3600)],
            render=lambda r: f"renewed {r['dut']} for {r['holder']}: {r['expires_in_s']}s left"),
        cmd("dut.present", "Whether the DUT's download/fastboot port is on the USB bus", mod.present,
            [DUT], mutating=False,
            render=lambda r: f"{r['dut']}: {'present' if r['present'] else 'not on bus'} ({r['usb']})"
                             + (f" {r['fastboot']['product']}" if r.get("fastboot") else "")),
        cmd("dut.stage", "Load an uploaded file into RAM through the download port (fastboot stage + continue)",
            lambda dut, sha256, lease, timeout, **kw: mod.stage(dut, sha256, lease, kw["continue"], timeout),
            [DUT, Param("sha256", help="sha256 of a file uploaded with PUT /api/v1/artifacts/<sha256>",
                        positional=False), LEASE,
             Param("continue", "boolean", "run `fastboot continue` after the stage", required=False, default=True),
             Param("timeout", "number", "seconds for the stage", required=False, default=300)],
            render=lambda r: f"{r['dut']}: staged {r['file']} ({r['size']:,} B)"
                             + (" and continued" if r["continued"] else "")),
        cmd("dut.boot_files", "Boot from RAM with uploaded files in order (FSBL, U-Boot/OpenSBI, kernel)",
            mod.boot_files,
            [DUT, Param("files", help="comma list of sha256 of uploaded files, in boot order", positional=False),
             LEASE,
             Param("reset", "boolean", "enter download mode first", required=False, default=True),
             Param("first_wait", "number", "seconds before polling for the port after the first file",
                   required=False, default=8),
             Param("later_wait", "number", "same, after later files", required=False, default=3),
             Param("port_timeout", "number", "how long to poll for the port to come back", required=False,
                   default=20)],
            render=lambda r: f"job {r['job']}: {r['dut']} boot {len(r['files'])} file(s)",
            description="Each file: fastboot stage + continue, then wait for the download port to come back "
                        "before the next. Background job: follow it with `bsp job get`. Never flashes."),
        cmd("artifact.has", "Whether a file with this sha256 is in the host's artifact cache",
            mod.artifact_has, [Param("sha256", help="64 hex digits")], mutating=False,
            render=lambda r: f"{r['sha256'][:16]}...: " + (f"cached ({r['size']:,} B, {r['file']})"
                                                            if r["present"] else "not cached")),
        cmd("dut.log", "Recent console lines (passive: no keystrokes, no lease)", mod.log,
            [DUT, Param("lines", "integer", "how many", required=False, default=40),
             Param("grep", help="regex filter", required=False)],
            render=lambda r: "\n".join(r["lines"]), mutating=False),
        cmd("dut.add", "Register a DUT on this host from a JSON spec (what probe writes)", mod.add,
            [Param("spec", "string", "JSON object: id, board, console{alias}, fastboot{usb}, control{...}, sdks"),
             Param("replace", "boolean", "overwrite an existing id", required=False, default=False)],
            render=_render_status),
        cmd("dut.remove", "Forget a probed/added DUT", mod.remove,
            [DUT, Param("force", "boolean", "even if leased", required=False, default=False)]),
        cmd("dut.rename", "Rename a DUT (aliases follow; console alias too if it was the DUT name)",
            mod.rename,
            [DUT, Param("new", help="new DUT name"), LEASE,
             Param("console", "boolean", "also rename the serial-mux console alias", required=False, default=False)],
            render=_render_status),
        cmd("dut.set", "Edit a DUT: board, soc, description, sdks, usb, reset pin, FDL strategy",
            lambda dut, lease, **f: mod.set_fields(dut, lease, **f),
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
        cmd("dut.logins", "ssh console logins on this host: `ssh <login>@<host>` opens that DUT's console",
            mod.login_list, mutating=False,
            render=lambda r: ("\n".join(f"{x['login']:<24} -> {x['dut']}" for x in r["logins"])
                              or "no console logins (modules.dut.ssh_console.enabled is off)")
            + (f"\nlast sync error: {r['error']}" if r.get("error") else "")),
        cmd("dut.export", "All DUT specs on this host (save into <site>/duts/ to make them static)",
            mod.export, mutating=False,
            render=lambda r: __import__("yaml").safe_dump(r, sort_keys=False, allow_unicode=True)),
        cmd("dut.probe", "Discover boards on this host: pulse free GPIOs, watch consoles + USB, identify",
            mod.probe,
            [Param("pins", help="comma list of candidate reset pins (default: all free header GPIOs)", required=False),
             Param("consoles", help="comma list of console candidates (serial-mux alias or /dev/tty*)", required=False),
             Param("identify", "boolean", "boot U-Boot to read board identity (TLV)", required=False, default=True),
             Param("sdks", help="comma list of sdks the found boards run (default by soc)", required=False),
             Param("dry_run", "boolean", "only show candidates, touch nothing", required=False, default=False),
             Param("human", "boolean", "no GPIO: a person resets each unregistered board into download mode",
                   required=False, default=False),
             Param("rounds", "integer", "human: at most this many boards", required=False, default=8),
             Param("wait_s", "number", "human: seconds to wait for each board", required=False, default=120)],
            render=_render_probe,
            description="Hook-driven (hooks/probe/*). Results are proposals: confirm with `bsp dut add`."),
    ]


# ================================================================= renders --
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
