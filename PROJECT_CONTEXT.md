# PROJECT_CONTEXT — NetCmd

## Project
NetCmd is a Python 3.10+ multivendor SSH command runner using Netmiko and Rich.

## Current Version
1.2.0

## Environment
Windows / PowerShell; Python 3.10 or later; dependencies in `requirements.txt`.

## Input Modes
- Single: `-ho` plus `-cmd` / `-cf`
- Standard batch: `-hf` plus `-cmd` / `-cf`
- Multifile: `-mf` with `HostName:` and optional `DeviceType:` blocks

## Features
- Configurable `-dt / --device-type`; per-host override in Multifile
- `-w / --workers` concurrent device execution (20 by default)
- Interactive workflow directives: `@timeout`, `@expect`, `@send`, `@wait`, `@wait-disconnect`, `@reconnect`
- `--mode exec/config`, `--dry-run`, `--verbose`, and `--version`
- Output filename modifiers: `--prefix`, `--suffix`, `--timestamp`
- Result TXT: commands and raw responses only
- Logs directory: full event log and summary log; no CSV

## Files
`netcmd.py`, `README.md`, `requirements.txt`, `examples/`, `tests/`, `.gitignore`.

## Constraints
Interactive operations, upgrades and reconnects require lab validation on each platform. The parser and offline simulated SSH tests do not guarantee platform-specific behavior. Keep real secrets, device inventories and collected output out of GitHub.

## Release
Initial publication of v1.2.0 in the public GitHub repository.
