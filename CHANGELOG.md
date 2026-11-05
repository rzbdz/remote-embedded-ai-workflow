# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) from 1.0 on.

## [Unreleased]

### Added
- `bsp dut bind`: register a board from a serial console and a USB port, without GPIO wiring.
- `human` control backend (alias `manual`): bsp asks a person to reset the board and continues on
  `bsp dut confirm` or when the board appears in download mode.
- Console logins: `ssh bsp-<dut>@<host>` opens a board's console without a password (opt-in,
  managed and cleaned up by the host). `bsp dut logins` lists them.
- `nudge` option for `wait` steps, for shell prompts printed without a newline.
- `bsp hints` and site hints in the `SKILL.md` written by `bsp install`.

### Changed
- Configuration is split into core, packs (`packs/`) and an external site directory
  (`BSP_SITE`); the repository no longer contains installation-specific data.
- The command, package and services are named `bsp`, `bsp-hostd` and `bsp-*`.
- `bsp-hostd` listens on `127.0.0.1` unless `server.bind` says otherwise.
- The web console is in English.

## [0.1.0b0]

First public beta: board leases, remote SDK workspaces and builds, boot recipes (RAM boot and
flash), board discovery, GPIO relay control with a web console, multi-host CLI, agent skills and
the end-to-end acceptance test.
