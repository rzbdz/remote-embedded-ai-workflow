# CLI reference

`bsp` is one command for people and programs. Every command accepts `--help`
(`bsp dut boot --help`).

## Global options

They may appear anywhere on the command line.

| Option | Meaning |
|---|---|
| `-r, --remote NAME[,NAME…]` | Board host(s) to talk to (names from `remotes.json`; unique prefixes work; `all` for every host) |
| `--url URL` | Talk to the host at this URL |
| `--token TOKEN` | API token for hosts that require one |
| `--json` | Machine-readable output (see [Output](#output-and-errors)) |
| `--stateless` | Ignore the saved default host and saved lease tokens; for parallel agents and CI |

How hosts are chosen without `-r`/`--url` is described in
[configuration.md](configuration.md#which-hosts-a-command-uses).

## Boards: `bsp dut`

| Command | Lease | Purpose |
|---|---|---|
| `bsp dut list [--sdk SDK] [--soc SOC] [--free]` | | Boards on all reachable hosts |
| `bsp dut status DUT` | | Stage, lease, fastboot state, control lines, console tail |
| `bsp dut log DUT [--lines N] [--grep RE]` | | Recent console lines, read passively |
| `bsp dut acquire DUT [--holder NAME] [--purpose TEXT] [--ttl S]` | | Lease a board; the token is remembered |
| `bsp dut release DUT [--force]` | ✓ | Release it |
| `bsp dut reset DUT [--no-fdl]` | ✓ | Reset into download mode and wait for it (`--no-fdl`: plain reset) |
| `bsp dut boot DUT --sdk SDK [--method load\|download] [--variant V] [--no-wait]` | ✓ | Run the SDK's boot recipe; streams progress, exit code is the result |
| `bsp dut console DUT "CMD"` | ✓ | Run one shell command on the console and print its output |
| `bsp dut console DUT` | | Attach interactively over ssh (Ctrl+] detaches) |
| `bsp dut confirm DUT [NOTE]` | | Tell a waiting reset/boot that the requested manual action is done |
| `bsp dut logins` | | `ssh` commands that open each board's console (if enabled on the host) |
| `bsp dut bind NAME --console TTY\|ALIAS --usb USB [--soc SOC] [--host HOST]` | | Register a board from a console and a USB port; a person does the resets |
| `bsp dut add HOST --spec JSON\|@FILE [--replace]` | | Register a board from a spec (what probe produces) |
| `bsp dut set DUT [--board/--soc/--description/--sdks/--usb/--reset-pin/--pulse-ms/--fdl/--fdl-pin]` | ✓ | Edit a board |
| `bsp dut rename OLD NEW [--console]` | ✓ | Rename a board (and its console alias with `--console`) |
| `bsp dut remove DUT [--force]` | | Forget a board |
| `bsp dut export` | | All boards as YAML, ready for `SITE/duts/` |

"Lease ✓" means the command needs your lease (the CLI passes the remembered token; use
`--lease TOKEN` or `BSP_LEASE` in stateless mode).

### Boards reset by a person

Boards with the `human` control backend (for example slots made with `bsp dut bind`) have no
reset wiring. `bsp dut reset` and `bsp dut boot` then print what to do and wait:

```console
$ bsp dut reset slot-1
slot-1 @ lab-1: reset needs a person, job reset-1793860731-1 (confirm with `bsp dut confirm slot-1`)
  [   0.0s] ACTION NEEDED: press RESET on slot-1 (board-x); it boots into download mode
  [   0.0s] waiting for `bsp dut confirm slot-1` or for the board to show it on its own (timeout 900s)
```

The wait ends when someone runs `bsp dut confirm slot-1`, or as soon as bsp sees the board in
download mode on its console or USB port (for a plain reset: the SoC's boot-ROM output on the
console). If the board is not actually in download mode after a confirmation, the next step
fails with a fastboot timeout. Scripts and agents should run such commands in the background.

## Discovery and hosts

| Command | Purpose |
|---|---|
| `bsp hosts` | Reachable hosts, how they are reached, their modules and boards |
| `bsp init` | First-run check: configuration directory, mode, hosts |
| `bsp probe [--pins P..] [--consoles C..] [--dry-run] [--yes] [--no-identify] [--replace]` | Discover boards (pulses GPIO lines — see [getting-started.md](getting-started.md#option-a-probe-boards-wired-to-gpio-reset-lines)) |
| `bsp remote list\|show\|add\|remove\|use` | Manage personal host entries |
| `bsp watch` | Stream live events from the host(s) |
| `bsp tools` | Print the hosts' AI tool schemas |

## SDKs: `bsp sdk`

| Command | Lease | Purpose |
|---|---|---|
| `bsp sdk list` | | SDK targets, their SoC, workspace and boot-method status |
| `bsp sdk show SDK` | | Build server, path, dev repos, components, artifacts, sharing |
| `bsp sdk status SDK` | | Lease, branches, uncommitted files, recent builds |
| `bsp sdk plan SDK [COMPONENT] [--boot load\|download]` | | Dry run: the build command or the boot steps |
| `bsp sdk acquire SDK --task NAME [--holder NAME]` | | Lease the workspace and switch to branch `bsp/NAME` |
| `bsp sdk release SDK [--keep] [--force]` | ✓ | Restore the owner's branch and stashed work |
| `bsp sdk build SDK COMPONENT [--clean]` | ✓ | Build on the server; streams warnings and errors |
| `bsp sdk jobs SDK`, `bsp sdk job SDK ID [--follow] [--log]` | | Build jobs |
| `bsp sdk exec SDK [--cwd DIR] [--read-only] -- CMD…` | ✓ | Run a command in the tree (cwd: first dev repo) |
| `bsp sdk get SDK PATH [-o FILE]`, `bsp sdk put SDK PATH FILE` | `put` ✓ | Read or write a file in the tree |

See [sdk.md](sdk.md) for what acquire and release do to the tree.

## Host commands

These run on a board host; the CLI fetches their definitions from the host, so they are always
in sync with it. Add `-r HOST` when several hosts are reachable.

| Group | Commands |
|---|---|
| `bsp gpio` | `list`, `get`, `set`, `pulse`, `free`, `config`, `export`, `pinmux`, `alias add\|delete\|list\|resolve` |
| `bsp relay` | `on`, `off`, `toggle`, `pulse` |
| `bsp button` | `press`, `release`, `click` |
| `bsp board` | `info` |
| `bsp job` | `list`, `get` |
| `bsp events` | `recent` |
| `bsp status` | Service, module and backend health |

The same commands are HTTP endpoints (`GET` for reads, `POST` for changes, under `/api/v1/`) and
AI tools; `GET /api/v1/tools` lists them and `/docs` serves the OpenAPI description.

## Output and errors

Text output is meant for people. With `--json` every command prints exactly one object:

```json
{"ok": true, "result": {"...": "..."}}
{"ok": false, "error": {"code": "not_found", "message": "no DUT 'example-bord' on any reachable host",
                        "hint": "did you mean: example-board? known: example-board@lab-1", "details": {}}}
```

`hint` names the next step (a likely typo, who holds a lease, which command to run); `details`
carries context such as the console tail or the end of a build log. Long-running commands print
progress lines to stderr in JSON mode.

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | The command failed |
| 2 | Usage error, or an ambiguous host for a state-changing command |
| 3 | No host reachable |
| 130 | Interrupted |
