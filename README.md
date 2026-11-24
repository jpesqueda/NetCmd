# NetCmd v1.2.1

Multivendor SSH command runner for Windows and other Python 3.10+ systems. Netmiko and Rich. Executes commands on individual devices or in parallel (20 workers by default). Supports Cisco IOS-XE, NX-OS, Arista EOS, and other Netmiko drivers. Command syntax is device-specific.

## Installation

```powershell
py -m pip install -r requirements.txt
py netcmd.py --version
```

For an installed PyPI distribution, `netcmd --version` uses the CLI entry point.

## Modes

```powershell
# Interactive wizard: host/host file, commands, username, password, suffix
netcmd

# Single host
netcmd -ho 192.0.2.10 -cmd commands.txt -u admin -dt cisco_ios

# Multiple devices, identical commands
netcmd -hf hosts.txt -cf commands.txt -u admin -dt cisco_ios -w 20

# Multifile, device-specific commands
netcmd -mf multifile.txt -u admin -w 20

# SSH-only test: no commands and no result .txt files
netcmd -hf hosts.txt -u admin -dt cisco_ios --test-connection

# Alternate SSH port
netcmd -ho 192.0.2.10 -cmd commands.txt -u admin --port 2222

# OpenSSH config file (including compatible ProxyCommand setups)
netcmd -ho router-alias -cmd commands.txt -u admin --ssh-config ~/.ssh/config

# Explicit key verification with user's default known_hosts
netcmd -ho 192.0.2.10 -cmd commands.txt -u admin --strict-host-key

# Explicit custom known_hosts (combine with --strict-host-key to reject unknown keys)
netcmd -ho 192.0.2.10 -cmd commands.txt -u admin --known-hosts ~/.ssh/known_hosts --strict-host-key
```

Use `-cmd`, `-cf`, or `--command-file` as synonyms. `--prefix`, `--suffix`, and `--timestamp` change **output filenames only**. `--verbose` prints device-level results as each worker finishes. Without `--verbose`, only connection failures appear per-device on screen.

### Multifile

```text
HostName:SW01
DeviceType:cisco_ios
show version
show inventory
!
HostName:SW02
DeviceType:arista_eos
show version
show interfaces status
!
```

### Interactive command directives

The same parser works with `-cmd` and `-mf`:

```text
@timeout 60
copy running-config startup-config
@expect Destination filename
@send [Enter]
```

Directives also include `@wait`, `@wait-disconnect`, and `@reconnect`. Always validate prompts and procedures on your hardware and firmware before disruptive execution. Timeouts do not guarantee that a command has finished safely.

## Output and logs

Result text files now resemble a live CLI session:

```text
SW01#show version
Cisco IOS XE Software ...

SW01#show module
Module  Status
1       ok
```

The prompt is read from the device's live CLI session rather than reconstructed from the input hostname. The capture removes only the first duplicated echo and a trailing standalone prompt; interactive prompts and CLI error text are preserved. If no CLI command has produced a response, **no empty TXT is written**.

```text
Outputs/
├── Results/
│   ├── pre_SW01_checks_YYYYMMDD_HHMMSS.txt
│   └── pre_SW02_checks_YYYYMMDD_HHMMSS.txt
└── Logs/
    ├── NetCmd_YYYYMMDD_HHMMSS.log
    └── Summary_YYYYMMDD_HHMMSS.log
```

No CSV or JSON reports are generated. `--test-connection` writes only logs, not result `.txt` files. At the end, the terminal shows a green `Status: All Success` when all devices succeeded; otherwise it shows red `Some Commands Failed` or `Some Devices Failed`. Authentication failure and unresolved hostnames are shown in the terminal even without verbose mode.

## Connection options

| Option | Purpose |
|---|---|
| `--port` | Destination SSH port (default 22) |
| `--conn-timeout` | TCP/SSH connect timeout |
| `--timeout` | Per-command timeout |
| `-dt` | Netmiko device driver, otherwise SSH autodetection |
| `--ssh-config` | Path to OpenSSH config used by Netmiko (ProxyCommand support; compatibility depends on Netmiko) |
| `--known-hosts` | Load a custom host key file |
| `--strict-host-key` | Reject unknown/mismatched SSH server host keys |
| `--test-connection` | Check SSH connectivity and authentication only; no commands |

**Security:** Prefer entering the password through the secure prompt rather than `-p` on the command line. Avoid committing real configuration outputs, private hostnames or credentials to public repositories. A host-key check protects against connecting to an unexpected server; ensure you trust the key material.

## Local tests

```powershell
py -m unittest discover -s tests -v
```

Offline tests cover command transcript formatting, error categorization, empty-output suppression, interactive prompting, and test-connection mode. Live SSH integration must still be tested against representative devices before any production change.
