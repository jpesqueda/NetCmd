# NetCmd — Multivendor Network Command Runner

**Version 1.2.0** · Python 3.10+ · SSH / Netmiko

NetCmd executes CLI commands over SSH on a single network device or on multiple devices concurrently. It supports Cisco IOS/IOS-XE, NX-OS, Arista EOS, and other drivers supported by Netmiko. Interactive workflows can wait for CLI prompts, send responses, and reconnect after a planned reboot.

> **Safety:** Run disruptive workflows in a lab first. NetCmd sends commands that you provide; it does not independently prove that an upgrade is safe or successful.

## Installation (Windows PowerShell)

```powershell
py -m pip install -r requirements.txt
py netcmd.py --help
py netcmd.py --version
```

## Run commands

**Single device**

```powershell
py netcmd.py -ho 192.0.2.11 -cmd examples/commands.txt -u admin -dt cisco_ios
```

**Same commands on multiple devices**

```powershell
py netcmd.py -hf examples/hosts.txt -cmd examples/commands.txt -u admin -dt cisco_ios -w 20
```

**Different commands per device (Multifile)**

```powershell
py netcmd.py -mf examples/multifile.txt -u admin -w 20
```

**Preview without connecting**

```powershell
py netcmd.py -mf examples/multifile.txt --dry-run
```

**Customize result names**

```powershell
py netcmd.py -mf examples/multifile.txt --prefix pre --suffix checks --timestamp -o Network_Audit
```

## Multifile input

```text
HostName:192.0.2.11
DeviceType:cisco_ios
show version
show inventory
!

HostName:192.0.2.12
DeviceType:arista_eos
show version
show interfaces status
!
```

Each device has its own command sequence. Repeated hostname blocks are combined in their original order. The `DeviceType:` directive overrides the CLI default driver. The host file and command file are used together in standard mode; `-mf` replaces both.

## Interactive commands

The same directives work in `-cmd` and `-mf`.

```text
@timeout 60
copy running-config startup-config
@expect Destination filename
@send [Enter]

@timeout 60
copy running-config bootflash:Config_2026_11_11
@expect Destination filename
@send [Enter]
```

Available workflow directives: `@timeout`, `@expect`, `@send`, `@wait`, `@wait-disconnect`, `@reconnect`. An `@expect` must match before its corresponding `@send` is transmitted. `@send y` sends `y` plus Enter; `@send [Enter]` sends only Enter.

For a planned reboot, a sequence could include:

```text
@wait-disconnect 300
@reconnect 900
```

Verify CLI prompt wording and timeout values on your actual device platform before use. A reconnect alone does not validate upgrade success.

## Output

```text
Outputs/
├── Results/
│   ├── pre_SW01_checks_YYYYMMDD_HHMMSS.txt
│   └── pre_SW02_checks_YYYYMMDD_HHMMSS.txt
└── Logs/
    ├── NetCmd_YYYYMMDD_HHMMSS.log
    └── Summary_YYYYMMDD_HHMMSS.log
```

Result `.txt` files contain commands and original CLI responses only. Complete diagnostics and an execution summary go into separate `.log` files. No CSV or JSON reports are generated.

The terminal normally shows NET COMMAND RUNNER, mode, device count, start/end time and a Rich progress bar. Use `--verbose` to show each completed device.

## Test

```powershell
py -m unittest discover -s tests -v
```

These tests are offline and do **not** establish connections to real network devices. Always validate interactive CLI behaviors against your specific firmware and model.

## Security

Use password prompts instead of passing credentials in the command line where practical. Never commit real hostnames, IP inventories, credentials, private configuration backups or production output logs. Example IP addresses use the documentation-only `192.0.2.0/24` range.

## Repository

Maintained as `jpesqueda/NetCmd`. This source tree contains release 1.2.0; see `PROJECT_CONTEXT.md` for implementation context.
