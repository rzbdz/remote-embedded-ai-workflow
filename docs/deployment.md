# Deployment

## Board hosts: `deploy/deploy.sh`

```bash
deploy/deploy.sh HOST [deploy|status|logs|restart|ssh] [--allow-dirty]
```

| Action | Effect |
|---|---|
| `deploy` (default) | Ship code and site, install, (re)start the service, health check |
| `status` | `systemctl status bsp-hostd` and the service's status endpoint |
| `logs` | The last `LINES` (default 100) journal lines of the service |
| `restart` | Restart the service |
| `ssh` | Open a shell on the host |

The script reads `SITE/deploy/HOST.env` and requires `SITE/hosts/HOST.yaml`. It ships the
committed tree of this repository (including the serial-mux submodule); `--allow-dirty` also
ships uncommitted changes and new, non-ignored files. The site directory is shipped as a whole,
minus `.git` and the patterns in `SITE/.bspignore`.

### Target file (`SITE/deploy/HOST.env`)

| Key | Meaning |
|---|---|
| `SSH_HOST`, `SSH_USER`, `SSH_PORT` | How to log in to the host (the installer needs root) |
| `SSH_JUMP` | Optional `user@host` to reach the target through a jump host |
| `INSTALL_DIR` | Installation directory, e.g. `/opt/bsp-dev-loop` |
| `SERVICE_PORT` | Port of the service (must match `server.port`) |
| `HEALTH_URL` | URL this machine uses for the health check (default: `EXTERNAL_URL`) |
| `EXTERNAL_URL` | URL printed at the end, for people |
| `UPNP_PORTS` | Router port mappings to register via UPnP; leave empty (see below) |

Authentication uses your ssh keys. For hosts that only accept a password, export `SSHPASS` and
install `sshpass`; the password is never written to disk by bsp.

### What is installed

| Path / unit | Contents |
|---|---|
| `INSTALL_DIR/app` | The shipped bsp tree (`app.old` keeps the previous one) |
| `INSTALL_DIR/site` | The shipped site (`site.old` keeps the previous one) |
| `INSTALL_DIR/venv` | Python environment with bsp and serial-mux |
| `/usr/local/bin/bsp` | The CLI, in local mode on the host |
| `bsp-hostd.service` | The service: `bsp-hostd --config INSTALL_DIR/site/hosts/HOST.yaml` with `BSP_SITE` set |
| `bsp-console-<alias>` | One transient unit per serial console, started on demand |
| `/var/lib/bsp` (`server.state_dir`) | Leases, probed boards, runtime GPIO settings, artifact cache |

With console logins enabled, the service also manages `bsp-*` system users, a `bsp-console`
group, `/etc/ssh/sshd_config.d/50-bsp-console.conf`, `/etc/sudoers.d/bsp-console` and helpers in
`/usr/local/lib/bsp/`; disabling the option removes all of them (see
[configuration.md](configuration.md#console-logins)).

### UPnP

For a host behind a home or lab router, `UPNP_PORTS` registers port mappings with a systemd timer
(`bsp-upnp.timer`). This exposes the API beyond the local network: only use it with
`server.auth_token` set, and prefer a VPN or an ssh `remotes.json` entry instead.

## Build-server access: `deploy/grant-build-access.sh`

```bash
deploy/grant-build-access.sh HOST USER@BUILD_SERVER [USER@BUILD_SERVER …]
```

Creates an ssh key for root on the board host (if missing) and authorises it on each build
server account, so the host can fetch build artifacts. This machine needs ssh access to both.
Use a dedicated build-server account if you do not want the host to have your own.

## Clients: `deploy/install-client.sh`

```bash
deploy/install-client.sh USER@MACHINE|local [DIR] [--site SITE_DIR] [--allow-dirty]
```

Copies the committed tree to `DIR` (default `~/bsp-dev-loop`), links `~/.local/bin/bsp`, and with
`--site` installs the site at `~/.config/bsp/site`. It uses ssh keys only (`BatchMode`). The
client needs Python ≥ 3.8 and PyYAML.

Alternatively, `pip install -e .` in a clone (Python ≥ 3.10) installs the `bsp` command with its
dependencies.

## Agent skills: `deploy/install-skills.sh`

```bash
deploy/install-skills.sh [USER@MACHINE|local]
```

Copies `skills/*` into the skill directories of the agents present on the machine: Claude Code
(`~/.claude/skills`), opencode (`~/.config/opencode/skill`) and Hermes
(`~/.hermes/skills/embedded`). See [agents.md](agents.md).
