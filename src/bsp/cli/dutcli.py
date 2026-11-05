"""`bsp dut ...`, `bsp probe`, `bsp hosts`, `bsp init` — fleet-aware board commands."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

from ..core.errors import InvalidRequest, BspError
from .fleet import Fleet, HostHandle, LeaseBook, config_dir
from .remotes import RemoteError


class CallError(BspError):
    def __init__(self, err: dict[str, Any], host: str):
        super().__init__(err.get("message", "error"), hint=err.get("hint"), details=err.get("details"))
        self.code = err.get("code", "error")
        self.host = host


def _ok(h: HostHandle, r: dict[str, Any]) -> Any:
    if not r.get("ok"):
        raise CallError(r.get("error") or {}, h.id)
    return r["result"]


def _lease(args, book: LeaseBook, dut: str) -> str | None:
    return args.lease or os.environ.get("BSP_LEASE") or book.get(f"dut:{dut}")


# -------------------------------------------------------------- hosts --
def hosts(fleet: Fleet, _args) -> tuple[Any, str]:
    hs = fleet.hosts()
    rows = [("HOST", "VIA", "MODULES", "DUTS")]
    out = []
    for h in hs:
        mods = h.status.get("modules", {})
        duts = (mods.get("dut") or {}).get("duts", [])
        rows.append((h.id, h.transport.label, ",".join(mods), ",".join(duts) or "-"))
        out.append({"host": h.id, "via": h.transport.label, "modules": list(mods), "duts": duts})
    for name, err in fleet.errors.items():
        rows.append((name, "UNREACHABLE", "", err[:60]))
        out.append({"host": name, "error": err})
    return out, _table(rows) + f"\n(mode: {fleet.mode})"


def init(fleet: Fleet, args) -> tuple[Any, str]:
    hs = fleet.hosts()
    cfg = config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    lines = [f"config dir: {cfg}", f"mode: {fleet.mode} ({'this machine is a board host' if fleet.mode == 'local' else 'talking to remote hosts'})"]
    for h in hs:
        lines.append(f"  host {h.id:<16} via {h.transport.label}  duts: {', '.join((h.status['modules'].get('dut') or {}).get('duts', [])) or '-'}")
    for n, e in fleet.errors.items():
        lines.append(f"  host {n:<16} UNREACHABLE ({e[:80]})")
    lines.append("next: `bsp probe` to discover boards, `bsp dut list`, `bsp sdk list`")
    return {"mode": fleet.mode, "hosts": [h.id for h in hs], "unreachable": fleet.errors}, "\n".join(lines)


# ----------------------------------------------------------------- dut --
def dut_list(fleet: Fleet, args) -> tuple[Any, str]:
    items = []
    for h, r in fleet.fan("dut_list"):
        if r.get("ok"):
            items += r["result"]
        elif "unknown command" in r["error"]["message"]:
            fleet.errors[h.id] = "dut module not enabled on this host"
        else:
            fleet.errors[h.id] = r["error"]["message"]
    if getattr(args, "soc", None):
        items = [d for d in items if d.get("soc") == args.soc]
    if getattr(args, "sdk", None):
        items = [d for d in items if args.sdk in d.get("sdks", [])]
    if getattr(args, "free", False):
        items = [d for d in items if not d["lease"]]
    rows = [("DUT", "HOST", "SOC", "BOARD", "STAGE", "LEASE", "USB", "CONSOLE", "SDKS")]
    for d in items:
        l = d["lease"]
        rows.append((d["id"], d["host"], d.get("soc") or "-", d["board"],
                     d["stage"] + (f" [{d['busy']}]" if d.get("busy") else ""),
                     f"{l['holder']} ({l['expires_in_s']}s)" if l else "free", d["usb"], d["console"],
                     ",".join(d["sdks"])))
    text = _table(rows) if items else "(no DUTs — run `bsp probe`)"
    if fleet.errors:
        text += "\nunreachable: " + ", ".join(f"{k} ({v[:50]})" for k, v in fleet.errors.items())
    return items, text


def dut_cmd(fleet: Fleet, args, book: LeaseBook) -> tuple[Any, str]:
    c, dut = args.dut_cmd, args.dut
    h = fleet.find_dut(dut)
    if c == "status":
        r = _ok(h, h.call("dut_status", {"dut": dut}))
        return r, _status_text(r)
    if c == "acquire":
        r = _ok(h, h.call("dut_acquire", {"dut": dut, "holder": args.holder, "ttl": args.ttl,
                                           "purpose": args.purpose or "", "lease": _lease(args, book, dut)}))
        book.put(f"dut:{dut}", r["token"], r["expires_in_s"], host=h.id, holder=r["holder"])
        return r, f"leased {dut} on {h.id} to {r['holder']} for {r['expires_in_s']}s\ntoken: {r['token']}"
    if c == "release":
        r = _ok(h, h.call("dut_release", {"dut": dut, "lease": _lease(args, book, dut), "force": args.force}))
        book.drop(f"dut:{dut}")
        return r, f"{dut}: {'released' if r['released'] else r.get('message')}"
    if c == "reset":
        r = _ok(h, h.call("dut_reset", {"dut": dut, "lease": _lease(args, book, dut), "fdl": not args.no_fdl},
                          timeout=120))
        return r, "\n".join(r["log"]) + f"\nfastboot: {r['fastboot']}"
    if c == "boot":
        r = _ok(h, h.call("dut_boot", {"dut": dut, "sdk": args.sdk, "method": args.method,
                                        "variant": args.variant, "lease": _lease(args, book, dut)}))
        out = sys.stderr if args.json else sys.stdout
        print(f"{dut} @ {h.id}: boot {args.sdk} [{args.method}] job {r['job']}", file=out, flush=True)
        if args.no_wait:
            return r, f"follow with: bsp job get {r['job']} -r {h.id} --wait 30"
        return follow_job(h, r["job"], out, args.timeout)
    if c == "console" and not args.command:
        return _attach(h, dut, args)
    if c == "console":
        cmd = " ".join(args.command)
        r = _ok(h, h.call("dut_console", {"dut": dut, "cmd": cmd, "expect": args.expect, "timeout": args.timeout,
                                           "lease": _lease(args, book, dut)}, timeout=args.timeout + 30))
        return r, r["output"]
    if c == "log":
        r = _ok(h, h.call("dut_log", {"dut": dut, "lines": args.lines, "grep": args.grep}))
        return r, "\n".join(r["lines"])
    if c == "remove":
        r = _ok(h, h.call("dut_remove", {"dut": dut, "force": args.force}))
        return r, f"removed {dut} from {h.id}"
    if c == "rename":
        r = _ok(h, h.call("dut_rename", {"dut": dut, "new": args.new, "console": args.console,
                                          "lease": _lease(args, book, dut)}, timeout=120))
        book.drop(f"dut:{dut}")
        return r, f"renamed {dut} -> {args.new} on {h.id}" + (f" ({r['note']})" if r.get("note") else "") \
            + "\n" + _status_text(r)
    if c == "set":
        fields = {k: getattr(args, k) for k in ("board", "soc", "description", "sdks", "usb", "reset_pin",
                                                 "pulse_ms", "fdl", "fdl_pin")}
        r = _ok(h, h.call("dut_set", {"dut": dut, "lease": _lease(args, book, dut), **fields}, timeout=60))
        return r, f"updated {dut}: {', '.join(r['changed'])}\n" + _status_text(r)
    raise InvalidRequest(f"unknown dut action {c}")


def follow_job(h: HostHandle, job: str, out, timeout: float) -> tuple[Any, str]:
    since, t0 = 0, time.time()
    while True:
        v = _ok(h, h.call("job_get", {"id": job, "since": since, "wait": 5}, timeout=60))
        for line in v["lines"]:
            print(f"  {line}", file=out, flush=True)
        since = v["next"]
        if v["status"] != "running":
            break
        if time.time() - t0 > timeout:
            raise BspError(f"job {job} still running after {timeout:g}s",
                           hint=f"keep following: bsp job get {job} -r {h.id} --wait 60")
    if v["status"] == "failed":
        e = v.get("error") or {}
        raise CallError({**e, "message": f"job {job} failed: {e.get('message')}"}, h.id)
    return v, f"job {job} succeeded in {v['elapsed_s']}s"


def _attach(h: HostHandle, dut: str, args) -> tuple[Any, str]:
    """Interactive console for humans: smtty on the host over ssh (Ctrl+] detaches)."""
    import shutil
    import subprocess

    st = _ok(h, h.call("dut_status", {"dut": dut}))
    target = getattr(h.transport, "target", None) or h.remote.ssh
    if not target:
        raise InvalidRequest(f"no ssh route to {h.id} for an interactive console",
                             hint=f"add one: bsp remote add {h.remote.name} {h.remote.url} --ssh root@<host>; "
                                  f"or run one command: bsp dut console {dut} 'uname -a'")
    if not sys.stdin.isatty():
        raise InvalidRequest("interactive console needs a terminal", hint=f"run one command: bsp dut console {dut} 'CMD'")
    ssh = shutil.which("ssh") or "ssh"
    print(f"attaching to {dut} console ({st['console']} on {h.id}); Ctrl+] detaches", file=sys.stderr)
    rc = subprocess.call([ssh, "-t", target, f"smtty {st['console']}"])
    return {"rc": rc}, ""


def export(fleet: Fleet, args) -> tuple[Any, str]:
    import yaml

    out = {}
    for h, r in fleet.fan("dut_export"):
        if r.get("ok"):
            out[h.id] = r["result"]["duts"]
    return out, yaml.safe_dump(out, sort_keys=False, allow_unicode=True)


def dut_add(fleet: Fleet, args) -> tuple[Any, str]:
    spec = args.spec
    if spec.startswith("@"):
        spec = open(spec[1:]).read()
    hs = [h for h in fleet.require() if h.id == args.host]
    if not hs:
        raise RemoteError(f"host {args.host!r} not reachable", hint=f"reachable: {', '.join(h.id for h in fleet.hosts())}")
    r = _ok(hs[0], hs[0].call("dut_add", {"spec": spec, "replace": args.replace}))
    return r, f"added {r['id']} on {args.host}\n" + _status_text(r)


# --------------------------------------------------------------- probe --
def probe(fleet: Fleet, args) -> tuple[Any, str]:
    params = {"pins": args.pins, "consoles": args.consoles, "identify": not args.no_identify,
              "sdks": args.sdks, "dry_run": args.dry_run}
    out = sys.stderr if args.json else sys.stdout
    jobs = []
    for h, r in fleet.fan("dut_probe", params):
        if not r.get("ok"):
            print(f"[{h.id}] probe not started: {r['error']['message']}", file=out)
            continue
        if args.dry_run:
            jobs.append((h, r["result"]))
            continue
        print(f"[{h.id}] probing (job {r['result']['job']}) ...", file=out, flush=True)
        jobs.append((h, r["result"]["job"]))
    if args.dry_run:
        return {h.id: res for h, res in jobs}, json.dumps({h.id: res for h, res in jobs}, indent=2, ensure_ascii=False)
    results: dict[str, Any] = {}
    for h, job in jobs:
        try:
            v, _ = follow_job(h, job, _Prefixed(out, f"[{h.id}]"), 900)
            results[h.id] = v["result"]
        except BspError as e:
            print(f"[{h.id}] probe failed: {e.message}", file=out)
    taken = {d["id"] for _, r in fleet.fan("dut_list") if r.get("ok") for d in r["result"]}
    interactive = sys.stdin.isatty() and not args.yes and not args.json
    added, lines = [], []
    for host, res in results.items():
        for p in res.get("proposals", []):
            spec = p["spec"]
            spec["id"] = _unique(spec["id"], taken)
            print(f"\n[{host}] found {spec['board']} ({spec['description']})\n"
                  f"    reset {spec['control']['reset']['pin']}  console {spec['console']['alias']}  "
                  f"usb {spec['fastboot']['usb']}  fdl {spec['control']['fdl']['strategy']}", file=out)
            for w in p.get("warnings", []):
                print(f"    warning: {w}", file=out)
            if interactive:
                ans = input(f"    name [{spec['id']}] (or 'skip'): ").strip()
                if ans == "skip":
                    continue
                if ans:
                    spec["id"] = ans
            elif not args.yes:
                lines.append(f"[{host}] proposal {spec['id']} not registered (pass --yes, or run interactively)")
                continue
            taken.add(spec["id"])
            h = next(x for x in fleet.hosts() if x.id == host)
            r = h.call("dut_add", {"spec": json.dumps(spec), "replace": args.replace})
            if r.get("ok"):
                added.append({"host": host, "dut": spec["id"]})
                lines.append(f"[{host}] registered {spec['id']}")
            else:
                lines.append(f"[{host}] {spec['id']} not registered: {r['error']['message']}"
                             + (f" ({r['error'].get('hint')})" if r['error'].get('hint') else ""))
        for c in res.get("unmatched_consoles", []):
            lines.append(f"[{host}] console {c['id']} did not react to any reset line")
    if not results:
        lines.append("no host completed a probe")
    return {"results": results, "added": added}, "\n".join(lines) or "nothing found"


class _Prefixed:
    def __init__(self, out, prefix):
        self.out, self.prefix = out, prefix

    def write(self, s):
        if s.strip():
            self.out.write(f"{self.prefix}{s if s.startswith(' ') else ' ' + s}")
        elif s:
            self.out.write(s)

    def flush(self):
        self.out.flush()


def _unique(name: str, taken: set) -> str:
    if name not in taken:
        return name
    n = 2
    while f"{name}-{n}" in taken:
        n += 1
    return f"{name}-{n}"


# --------------------------------------------------------------- views --
def _status_text(d: dict[str, Any]) -> str:
    l = d.get("lease")
    return "\n".join([
        f"{d['id']} @ {d['host']}: {d['board']} ({d['soc']}) — {d['description']}",
        f"  stage    : {d['stage']}" + (f"  busy: {d['busy']}" if d.get("busy") else ""),
        f"  lease    : {l['holder']} ({l['expires_in_s']}s left)" if l else "  lease    : free",
        f"  fastboot : {d['fastboot'] or 'not on bus'} (usb {d['usb']})",
        f"  control  : {d['control_state']}",
        f"  sdks     : {', '.join(d['sdks'])}",
        "  console  :", *[f"    {x}" for x in d.get("console_tail", [])],
    ])


def _table(rows) -> str:
    w = [max(len(str(r[c])) for r in rows) for c in range(len(rows[0]))]
    return "\n".join("  ".join(str(x).ljust(w[c]) for c, x in enumerate(r)).rstrip() for r in rows)


def add_parsers(sub) -> None:
    sub.add_parser("hosts", help="reachable bsp hosts, how they are reached, their DUTs")
    sub.add_parser("init", help="first-run check: mode (local/remote), reachable hosts, next steps")
    p = sub.add_parser("probe", help="discover boards on the host(s): reset lines, consoles, USB, identity")
    p.add_argument("--pins", help="candidate reset pins, e.g. P33,P11")
    p.add_argument("--consoles", help="candidate consoles (serial-mux alias or /dev/tty*)")
    p.add_argument("--sdks", help="sdks the found boards run (default per SoC)")
    p.add_argument("--no-identify", action="store_true", help="skip booting U-Boot to read the board identity")
    p.add_argument("--dry-run", action="store_true", help="only list candidates")
    p.add_argument("--yes", action="store_true", help="register proposals without asking (agents)")
    p.add_argument("--replace", action="store_true", help="overwrite DUTs with the same id")

    d = sub.add_parser("dut", help="boards: list/status, lease, reset, boot an SDK, serial console")
    ds = d.add_subparsers(dest="dut_cmd", metavar="<action>")
    dl = ds.add_parser("list", help="all DUTs on all reachable hosts")
    dl.add_argument("--soc", help="only this SoC (as named in the SoC profiles)")
    dl.add_argument("--sdk", help="only DUTs that can run this SDK")
    dl.add_argument("--free", action="store_true", help="only DUTs nobody leases")
    ds.add_parser("export", help="all DUT specs (YAML, ready for configs/duts/)")
    for name, hlp in (("status", "stage, lease, fastboot, control lines, console tail"),
                      ("log", "recent console lines (passive)")):
        x = ds.add_parser(name, help=hlp)
        x.add_argument("dut")
        if name == "log":
            x.add_argument("--lines", type=int, default=40)
            x.add_argument("--grep")
    a = ds.add_parser("acquire", help="lease a DUT (token remembered for later commands)")
    a.add_argument("dut")
    a.add_argument("--holder", default=os.environ.get("BSP_HOLDER") or os.environ.get("USER") or "anonymous")
    a.add_argument("--ttl", type=int, default=3600)
    a.add_argument("--purpose")
    a.add_argument("--lease")
    r = ds.add_parser("release", help="release a DUT lease")
    r.add_argument("dut")
    r.add_argument("--force", action="store_true")
    r.add_argument("--lease")
    rs = ds.add_parser("reset", help="reset (by default: into FDL download mode, waits for it)")
    rs.add_argument("dut")
    rs.add_argument("--no-fdl", action="store_true", help="plain reset, do not wait for FDL")
    rs.add_argument("--lease")
    b = ds.add_parser("boot", help="boot an SDK: --method load (RAM boot) | download (flash)")
    b.add_argument("dut")
    b.add_argument("--sdk", required=True)
    b.add_argument("--method", default="load", choices=["load", "download"])
    b.add_argument("--variant", help="e.g. uboot-only")
    b.add_argument("--no-wait", action="store_true")
    b.add_argument("--timeout", type=float, default=900)
    b.add_argument("--lease")
    c = ds.add_parser("console", help="run a shell command on the DUT console (no command: interactive attach)")
    c.add_argument("dut")
    c.add_argument("command", nargs="*", help="e.g. 'dmesg | grep mytag' (quote pipes); omit to attach")
    c.add_argument("--expect", help="completion regex (default: shell prompt)")
    c.add_argument("--timeout", type=float, default=20)
    c.add_argument("--lease")
    ad = ds.add_parser("add", help="register a DUT on a host from a JSON spec (what probe produces)")
    ad.add_argument("host")
    ad.add_argument("--spec", required=True, help="JSON or @file.json")
    ad.add_argument("--replace", action="store_true")
    rm = ds.add_parser("remove", help="forget a probed DUT")
    rm.add_argument("dut")
    rm.add_argument("--force", action="store_true")
    rn = ds.add_parser("rename", help="rename a DUT (its reset/fdl aliases follow)")
    rn.add_argument("dut")
    rn.add_argument("new")
    rn.add_argument("--console", action="store_true", help="also rename the serial-mux console alias")
    rn.add_argument("--lease")
    st = ds.add_parser("set", help="edit a DUT: board, soc, description, sdks, usb, reset pin, FDL")
    st.add_argument("dut")
    st.add_argument("--board")
    st.add_argument("--soc")
    st.add_argument("--description")
    st.add_argument("--sdks", help="comma list; '' = all SDKs of the board's SoC")
    st.add_argument("--usb", help="fastboot selector, e.g. usb:2-1.4")
    st.add_argument("--reset-pin", dest="reset_pin")
    st.add_argument("--pulse-ms", dest="pulse_ms", type=int)
    st.add_argument("--fdl", choices=["strapped", "button"])
    st.add_argument("--fdl-pin", dest="fdl_pin")
    st.add_argument("--lease")


def run(fleet: Fleet, args, book: LeaseBook):
    if args.cmd == "hosts":
        return hosts(fleet, args)
    if args.cmd == "init":
        return init(fleet, args)
    if args.cmd == "probe":
        return probe(fleet, args)
    if args.dut_cmd is None:
        raise InvalidRequest("missing dut action", hint="bsp dut --help")
    if args.dut_cmd == "list":
        return dut_list(fleet, args)
    if args.dut_cmd == "add":
        return dut_add(fleet, args)
    if args.dut_cmd == "export":
        return export(fleet, args)
    return dut_cmd(fleet, args, book)
