# NetCmd Project Context

Version: 1.2.1 (development release; not published on PyPI).

Platform: Windows Python 3.10+; Netmiko 4.x; Rich.

Supported: Standard host (-ho), host list (-hf) with common -cmd/-cf; Multifile (-mf) with HostName/DeviceType. CLI modes exec and config, CLI interaction directives @timeout, @expect, @send, @wait, @wait-disconnect, @reconnect; workers default 20.

V1.2.1 adds: prompt-driven interactive CLI, SSH test mode, port and SSH config, strict known_hosts options, visible SSH error classifications, connection status summary, no empty outputs.

Outputs: Results per device with only `prompt+command` and CLI output; Logs full and Summary; no CSV. Password prompt uses getpass; passwords are not written to logs by NetCmd.

Current limitations: Never assume vendor command correctness; regex-based CLI errors may need driver-specific adjustments; Netmiko accepts OpenSSH configuration with certain patterns such as ProxyCommand, but does not guarantee every SSH config directive. Device tests were simulated offline. No live production testing performed.
