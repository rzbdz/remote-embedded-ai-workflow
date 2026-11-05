# SDKs and boot recipes

An **SDK target** is something you can build and boot: a source tree on a build server, a build
style, the SoC it is for, where its build artifacts land, and how to put them on a board. All of
it is site configuration under `SITE/sdk/`.

```text
sdk/servers/<id>.yaml       build servers: ssh login and job environment
sdk/families/<id>.yaml      build styles: how to build a component, where artifacts land
sdk/workspaces/<id>.yaml    source trees shared by several targets (optional)
sdk/instances/<id>.yaml     SDK targets: what you name in `bsp sdk …` and `bsp dut boot --sdk`
```

Validate a change without hardware with `bsp sdk list`, `bsp sdk show SDK` and
`bsp sdk plan SDK [--boot load|download]`.

## Build servers

```yaml
id: build-a
host: 192.0.2.10
user: dev
description: shared build machine
home_aliases: [/home/dev, /data/home/dev]   # the same home under two paths (used by `bsp install`)
env:                                        # what an interactive login would set up, for build jobs
  path_prepend: ["~/toolchains/riscv/bin"]
  vars: {LD_LIBRARY_PATH: "~/.local/lib"}
```

bsp runs a small stdlib-only helper over ssh (Python ≥ 3.6); nothing is installed on the server.

## Families (build styles)

```yaml
id: make-bsp
build:
  runner: make                 # make | envsetup | script
  targets: {kernel: kernel, uboot: uboot, all: all}      # component -> make target
  clean: {kernel: clean_kernel}
artifacts:
  dir: output                  # relative to the tree; may use {field} of the instance
  roles:                       # role -> file name (globs pick the newest match)
    fsbl: FSBL.bin
    uboot-sbi: u-boot-opensbi.itb
    kernel-fit: Image.itb
dev_projects: [linux]          # git repositories inside the tree that a lease branches
console: {prompt: "# $"}       # shell prompt on the booted board
```

| Runner | Build command for component `C` |
|---|---|
| `make` | `cd TREE && make TARGETS[C]` |
| `envsetup` | `cd TREE && source ENVSETUP PLAT && TARGETS[C]` — `build.envsetup` names the script, the instance's `plat:` the platform, the target a shell function it defines |
| `script` | `cd TREE && TARGETS[C]`, with `{field}` replaced from the instance (e.g. `./build.sh -b {board}`) |

## Workspaces and leases on source trees

A **workspace** is one checkout on one server and the unit of mutual exclusion. Several targets
can share it — for example one upstream kernel tree built for two SoCs. A target without
`workspace:` owns an implicit workspace (its server and path).

```yaml
id: kernel-tree
server: build-a
path: /home/dev/work/kernel-tree
dev_projects: ["."]
```

What `bsp sdk acquire SDK --task NAME` does in each dev repository:

1. records the current branch and commit;
2. stashes uncommitted changes to tracked files (`git stash push`; untracked files stay);
3. checks out `bsp/NAME`, creating it from the current commit, or resumes it if it exists.

`bsp sdk release SDK` stashes whatever is still uncommitted on the task branch, checks out the
recorded branch and pops the stash from step 2. The task branch and its commits are kept.
Commit your work before releasing.

Lease state lives in the tree, in `TREE/.bsp/`:

| Path | Contents |
|---|---|
| `.bsp/lease.json` | Holder, task, branch, recorded state per repo, token, expiry (default 8 h) |
| `.bsp/jobs/<id>/` | Build job script, log, exit code, metadata |
| `.bsp/build.lock` | One build per workspace at a time |

bsp adds `/.bsp/` to `.git/info/exclude` when the tree root is a repository. If a lease was lost
(for example the token is gone), the holder information in `lease.json` tells you who had it;
`bsp sdk release SDK --force` releases it and restores the recorded state (`--keep` releases without switching branches).

## SDK targets

```yaml
id: bsp-a
family: make-bsp
server: build-a                 # or `workspace: <id>` to share a tree
path: /home/dev/work/bsp-a
soc: soc-a                      # boards of this SoC can run it
description: vendor BSP for soc-a
boot:
  load:                         # RAM boot; nothing is written to storage
    status: ready               # ready | todo
    steps: [...]
    variants: {uboot-only: stop once U-Boot answers fastboot}
  download:                     # flash to storage
    status: todo
    notes: "flash layout not described yet"
    steps: []
```

Both `load` and `download` must be present; an unfinished one is `status: todo` with `notes`,
which `bsp dut boot` shows instead of running anything.

## Boot recipe steps

Steps run in order on the board host. Each is a mapping with `do:` and its options.

| Step | Options | Effect |
|---|---|---|
| `enter-fdl` | `timeout` | Reset into download mode through the board's control backend, then wait until the SoC profile's download-mode console line and fastboot device appear |
| `reset` | | Plain reset |
| `stage` | `artifact`, `timeout`, `stop_if` | `fastboot stage` the artifact with that role |
| `continue` | | `fastboot continue` |
| `oem` | `cmd` | `fastboot oem CMD` |
| `getvar` | `name`, `match` | `fastboot getvar NAME`; fail unless it matches the regex |
| `flash` | `partition`, `artifact`, `timeout` | `fastboot flash PARTITION FILE` |
| `wait` | `serial`, `fastboot`, `fail`, `timeout`, `stall`, `nudge` | Wait for a console regex and/or a fastboot product regex. `fail`: regex that aborts; `stall`: seconds of console silence that count as failure; `nudge`: press Enter after that many quiet seconds (for prompts printed without a newline — not while a bootloader may treat a key as "stop autoboot") |
| `settle` | `seconds`, `why` | Fixed delay; state why |
| `hook` | `kind`, `name`, `artifacts`, `args`, `timeout` | Run `hooks/KIND/NAME` (e.g. a vendor flash tool); see [hooks/README.md](../hooks/README.md) |

Common to every step:

- `when: fdl-not-strapped` — skip on boards whose reset always enters download mode (they cannot
  cold-boot from storage, so a post-flash boot check is impossible there);
- `stop_if: VARIANT` — end the recipe here when that variant was requested
  (`bsp dut boot … --variant VARIANT`).

Before running, the host fetches every artifact the recipe refers to from the build server over
ssh and caches it by checksum, so unchanged files are not copied again.
