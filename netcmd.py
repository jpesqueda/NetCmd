#!/usr/bin/env python3
"""NetCmd v1.2.2: multivendor SSH command runner with interactive workflows.

Designed for Windows / Python 3.10+. No third-party package is imported when
showing --help, --version or validating an input file with --dry-run.
"""
from __future__ import annotations

import argparse
import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime
import getpass
import logging
from pathlib import Path
import re
import socket
import sys
import threading
import time
from typing import Any

VERSION = "1.2.2"
DEFAULT_TIMEOUT = 60.0
# Cisco IOS-XE, NX-OS and EOS prompts, including all CLI submodes.
# Anchoring to a single final line avoids treating earlier output as the next prompt.
PROMPT_PATTERN = re.compile(r"[A-Za-z0-9_.:/@~-]+(?:\([^\r\n()]{1,80}\))*[>#%$]", re.ASCII)
CONFIG_MODES = re.compile(r"\((?:config|conf)(?:[-/][^()]*)?\)", re.I)
CONFIG_ENTER = re.compile(r"^(?:conf(?:igure)?(?:\s+(?:terminal|t))?|config\s+t)$", re.I)
CONFIG_LEAVE = re.compile(r"^(?:end|disable)$", re.I)
ERROR_PATTERN = re.compile(
    r"(?im)^\s*(?:%\s*(?:invalid|error|incomplete|ambiguous|unknown|unrecognized|failed)|"
    r"error:\s|syntax error|invalid command|unknown command|command not found|"
    r"permission denied|not enough space|copy failed)"
)
WR_COMMANDS = {"wr", "write memory", "copy running-config startup-config"}
SENSITIVE = re.compile(r"(?i)\b(password|secret|token|community|private-key|passphrase)\b")
DISRUPTIVE = re.compile(r"(?i)^\s*(?:install\s+all\b|reload\b|request\s+system\s+reboot\b|reboot\b)")
HOST_RE = re.compile(r"^hostname\s*:\s*(\S+)\s*$", re.I)
TYPE_RE = re.compile(r"^devicetype\s*:\s*(\S+)\s*$", re.I)
VALID_DIRECTIVES = {"timeout", "expect", "send", "wait", "wait-disconnect", "reconnect"}
LOG_LOCK = threading.Lock()


class InputError(ValueError):
    """Bad command-file syntax, reported before any connections are made."""


class CommandFailure(RuntimeError):
    """Failure during a command; do not blindly replay the command."""


@dataclass
class CommandStep:
    command: str
    line: int
    timeout: float | None = None
    interactions: list[tuple[str, str]] = field(default_factory=list)
    wait_disconnect: float | None = None
    reconnect: float | None = None


@dataclass
class WaitStep:
    seconds: float
    line: int


@dataclass
class DeviceJob:
    host: str
    device_type: str | None
    steps: list[CommandStep | WaitStep] = field(default_factory=list)


@dataclass
class DeviceResult:
    host: str
    total: int
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    status: str = "FAILED"
    reason: str = ""
    output_path: str = ""
    connection_error: str = ""
    connection_details: str = ""


def positive_float(value: str) -> float:
    try:
        number = float(value)
        if 0 < number <= 86400:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be > 0 and <= 86400 seconds")


def positive_int(value: str) -> int:
    try:
        n = int(value)
        if n > 0:
            return n
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a positive integer")


def parse_steps(lines: list[tuple[int, str]], source: str) -> list[CommandStep | WaitStep]:
    steps: list[CommandStep | WaitStep] = []
    pending_timeout: float | None = None
    last: CommandStep | None = None
    waiting_for_send = False
    for lineno, line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("@"):
            key, _, value = stripped[1:].partition(" ")
            key, value = key.lower(), value.strip()
            if key not in VALID_DIRECTIVES:
                raise InputError(f"{source}:{lineno}: unknown directive @{key}")
            if not value:
                raise InputError(f"{source}:{lineno}: @{key} requires a value")
            if key in {"timeout", "wait", "wait-disconnect", "reconnect"}:
                try:
                    seconds = float(value)
                    if not (0 < seconds <= 86400):
                        raise ValueError()
                except ValueError:
                    raise InputError(f"{source}:{lineno}: @{key} requires seconds > 0 and <= 86400") from None
                if key == "timeout":
                    if waiting_for_send:
                        raise InputError(f"{source}:{lineno}: missing @send after @expect")
                    pending_timeout = seconds
                    last = None
                elif key == "wait":
                    if waiting_for_send:
                        raise InputError(f"{source}:{lineno}: missing @send after @expect")
                    steps.append(WaitStep(seconds, lineno))
                    last = None
                else:
                    if not last or waiting_for_send:
                        raise InputError(f"{source}:{lineno}: @{key} must follow a command and complete @expect/@send")
                    if key == "wait-disconnect":
                        if last.wait_disconnect is not None:
                            raise InputError(f"{source}:{lineno}: duplicate @wait-disconnect")
                        last.wait_disconnect = seconds
                    else:
                        if last.reconnect is not None:
                            raise InputError(f"{source}:{lineno}: duplicate @reconnect")
                        last.reconnect = seconds
            elif key == "expect":
                if not last or waiting_for_send or last.wait_disconnect is not None or last.reconnect is not None:
                    raise InputError(f"{source}:{lineno}: @expect must follow a command or completed @send")
                try:
                    re.compile(value)
                except re.error as exc:
                    raise InputError(f"{source}:{lineno}: invalid @expect regex: {exc}") from exc
                last.interactions.append((value, ""))
                waiting_for_send = True
            elif key == "send":
                if not last or not waiting_for_send:
                    raise InputError(f"{source}:{lineno}: @send requires a preceding @expect")
                pattern, _ = last.interactions[-1]
                last.interactions[-1] = (pattern, value)
                waiting_for_send = False
            continue
        if waiting_for_send:
            raise InputError(f"{source}:{lineno}: missing @send after @expect")
        if stripped == "!":
            raise InputError(f"{source}:{lineno}: '!' is only a separator in Multifile")
        last = CommandStep(stripped, lineno, timeout=pending_timeout)
        steps.append(last)
        pending_timeout = None
    if waiting_for_send:
        raise InputError(f"{source}: missing @send after final @expect")
    if pending_timeout is not None:
        raise InputError(f"{source}: @timeout without a subsequent command")
    for step in steps:
        if isinstance(step, CommandStep):
            if (step.wait_disconnect is None) != (step.reconnect is None):
                raise InputError(f"{source}:{step.line}: @wait-disconnect and @reconnect must be paired")
    return steps


def read_input(path: str) -> list[tuple[int, str]]:
    p = Path(path)
    try:
        return list(enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1))
    except OSError as exc:
        raise InputError(f"Cannot read {p}: {exc}") from exc


def load_multifile(path: str, *, require_commands: bool = True) -> list[DeviceJob]:
    raw = read_input(path)
    grouped: dict[str, DeviceJob] = {}
    buffers: list[tuple[int, str]] = []
    current: DeviceJob | None = None
    def flush() -> None:
        nonlocal buffers
        if current and buffers:
            current.steps.extend(parse_steps(buffers, path))
        buffers = []
    for lineno, line in raw:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        host_match = HOST_RE.fullmatch(stripped)
        if host_match:
            flush()
            hostname = host_match.group(1)
            key = hostname.casefold()
            if key not in grouped:
                grouped[key] = DeviceJob(hostname, None)
            current = grouped[key]
            continue
        type_match = TYPE_RE.fullmatch(stripped)
        if type_match:
            if current is None or buffers:
                raise InputError(f"{path}:{lineno}: DeviceType must follow HostName before commands")
            dtype = type_match.group(1)
            if current.device_type and current.device_type != dtype:
                raise InputError(f"{path}:{lineno}: conflicting DeviceType for {current.host}")
            current.device_type = dtype
            continue
        if stripped == "!":
            flush()
            current = None
            continue
        if current is None:
            raise InputError(f"{path}:{lineno}: command outside HostName: block")
        buffers.append((lineno, line))
    flush()
    if not grouped:
        raise InputError(f"{path}: no devices found")
    for device in grouped.values():
        if require_commands and not any(isinstance(s, CommandStep) for s in device.steps):
            raise InputError(f"{path}: {device.host} has no commands")
    return list(grouped.values())


def load_standard(host: str | None, host_file: str | None, commands_file: str | None, *, require_commands: bool = True) -> list[DeviceJob]:
    steps = parse_steps(read_input(commands_file), commands_file) if commands_file and require_commands else []
    if require_commands and not any(isinstance(s, CommandStep) for s in steps):
        raise InputError(f"{commands_file}: no commands")
    hosts = [host] if host else [line.strip() for _, line in read_input(host_file or "") if line.strip() and not line.lstrip().startswith("#")]
    unique = list(dict.fromkeys(h.casefold() for h in hosts))
    canonical = {h.casefold(): h for h in hosts}
    if not unique:
        raise InputError("No hosts found")
    return [DeviceJob(canonical[key], None, steps) for key in unique]


def build_parser() -> argparse.ArgumentParser:
    description = "NetCmd — multivendor SSH command runner (normal and Multifile modes)."
    examples = """Examples:
  python netcmd.py -ho SW01 -cmd examples/commands.txt -u admin
  python netcmd.py -hf examples/hosts.txt -cf examples/commands.txt -w 20
  python netcmd.py -mf examples/multifile.txt -u admin --dry-run
  python netcmd.py -mf examples/multifile.txt -u admin --verbose --timestamp
  python netcmd.py -hf examples/hosts.txt -cmd examples/interactive.txt -dt cisco_nxos -w 2 --yes
  python netcmd.py -hf examples/hosts.txt -u admin --test-connection -dt cisco_ios
  python netcmd.py -ho SW01 -cmd examples/commands.txt --port 2222
  python netcmd.py                     # interactive wizard

Interactive syntax: @timeout SEC (before command), @expect REGEX, @send TEXT,
                    @send [Enter], @wait SEC, @wait-disconnect SEC,
                    @reconnect SEC (both disconnect/reconnect must be paired).
@send TEXT automatically appends Enter. Do not add another @send [Enter].
"""
    p = argparse.ArgumentParser(description=description, epilog=examples, formatter_class=argparse.RawDescriptionHelpFormatter)
    devices = p.add_argument_group("device selection")
    sel = devices.add_mutually_exclusive_group(required=True)
    sel.add_argument("-ho", "--host", help="single hostname or IP")
    sel.add_argument("-hf", "--host-file", help="file listing hosts")
    sel.add_argument("-mf", "--multifile", help="HostName: / DeviceType: blocks with per-device commands")
    cmds = p.add_argument_group("command options")
    cmds.add_argument("-cf", "-cmd", "--command-file", dest="command_file", help="common command file (required with -ho/-hf)")
    cmds.add_argument("--mode", choices=["exec", "config"], default="exec", help="CLI mode (default: exec)")
    cmds.add_argument("--on-error", choices=["continue", "stop"], default="continue", help="on command error (default: continue)")
    auth = p.add_argument_group("authentication")
    auth.add_argument("-u", "--user", help="SSH username; prompt if omitted")
    auth.add_argument("-p", "--password", help="SSH password; prompt securely if omitted")
    auth.add_argument("--secret", help="enable secret (prompt when needed if omitted)")
    execution = p.add_argument_group("execution")
    execution.add_argument("-dt", "--device-type", help="Netmiko type, e.g. cisco_ios, cisco_nxos, arista_eos; default autodetect")
    execution.add_argument("-w", "--workers", type=positive_int, default=20, help="parallel devices (default: 20)")
    execution.add_argument("--timeout", type=positive_float, default=DEFAULT_TIMEOUT, help="per-command timeout seconds (default: 60)")
    execution.add_argument("--conn-timeout", type=positive_float, default=15, help="SSH connection timeout seconds (default: 15)")
    execution.add_argument("--port", type=positive_int, default=22, help="SSH TCP port (default: 22)")
    execution.add_argument("--ssh-config", help="path to OpenSSH config file (ProxyCommand supported by Netmiko)")
    execution.add_argument("--known-hosts", help="custom SSH known_hosts file")
    execution.add_argument("--strict-host-key", action="store_true", help="reject unknown SSH host keys; load user known_hosts")
    execution.add_argument("--test-connection", action="store_true", help="verify SSH and authentication only; do not run commands")
    execution.add_argument("--retries", type=int, default=1, help="extra connection retries (default: 1; never retries commands)")
    execution.add_argument("--dry-run", action="store_true", help="validate and preview without SSH")
    execution.add_argument("-y", "--yes", action="store_true", help="confirm potentially disruptive commands")
    execution.add_argument("--verbose", action="store_true", help="print per-device status beside progress bar")
    out = p.add_argument_group("output")
    out.add_argument("-o", "--output", default="Outputs", help="root output directory")
    out.add_argument("--prefix", default="", help="prepend text to result filename")
    out.add_argument("--suffix", default="", help="append text to result filename")
    out.add_argument("--timestamp", action="store_true", help="append timestamp to result filename")
    out.add_argument("--overwrite", action="store_true", help="replace existing result files")
    p.add_argument("-v", "--version", action="version", version=f"NetCmd {VERSION}")
    return p


def result_filename(host: str, args: Any, stamp: str) -> str:
    parts = [x for x in (args.prefix, host, args.suffix, stamp if args.timestamp else "") if x]
    name = "_".join(parts)
    if any(x in name for x in ("/", "\\", "..")) or not name or re.search(r'[<>:"|?*\x00-\x1f]', name):
        raise InputError("Invalid hostname/prefix/suffix for output filename")
    return name + ".txt"


def redact_command(command: str) -> str:
    return "[REDACTED COMMAND]" if SENSITIVE.search(command) else command


def setup_logger(path: Path) -> logging.Logger:
    logger = logging.getLogger("netcmd")
    for old_handler in list(logger.handlers):
        logger.removeHandler(old_handler)
        old_handler.close()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    return logger


def detect_and_connect(host: str, dtype: str | None, username: str, password: str, args: Any):
    from netmiko import ConnectHandler, SSHDetect
    base = dict(host=host, username=username, password=password, port=args.port,
                conn_timeout=args.conn_timeout, auth_timeout=args.conn_timeout,
                banner_timeout=args.conn_timeout, fast_cli=False,
                ssh_strict=args.strict_host_key, system_host_keys=args.strict_host_key)
    if args.ssh_config:
        base["ssh_config_file"] = str(Path(args.ssh_config).expanduser())
    if args.known_hosts:
        base["alt_host_keys"] = True
        base["alt_key_file"] = str(Path(args.known_hosts).expanduser())
    if args.secret:
        base["secret"] = args.secret
    resolved = dtype or args.device_type
    last_exc: Exception | None = None
    for attempt in range(args.retries + 1):
        try:
            if not resolved:
                guess = SSHDetect(device_type="autodetect", **base).autodetect()
                if not guess:
                    raise CommandFailure("Netmiko SSH autodetection failed; supply -dt or DeviceType")
                resolved = guess
            connection = ConnectHandler(device_type=resolved, **base)
            try:
                if not args.test_connection and connection.check_enable_mode() is False:
                    connection.enable()
            except (AttributeError, ValueError):
                pass  # Not every platform implements enable mode.
            return connection, resolved
        except Exception as exc:
            last_exc = exc
            # Never retry authentication failures or unknown drivers.
            message = type(exc).__name__.lower() + " " + str(exc).lower()
            if any(x in message for x in ("authentication", "auth_exception", "password", "unsupported device", "ssh autodetection", "name or service not known", "getaddrinfo failed")):
                break
            if attempt < args.retries:
                time.sleep(min(2 ** attempt, 5))
    raise CommandFailure(f"SSH connection failed: {type(last_exc).__name__}: {last_exc}")


def classify_connection_error(exc: BaseException) -> tuple[str, str]:
    """Map connection errors to safe, actionable terminal labels."""
    detail = str(exc)
    lower = (type(exc).__name__ + " " + detail).lower()
    if any(x in lower for x in ("authenticationexception", "netmikoauthenticationexception", "authentication failed", "auth failed", "bad authentication", "permission denied", "authentication failure")):
        return "AUTHENTICATION FAILED", detail
    if any(x in lower for x in ("gaierror", "getaddrinfo", "name or service not known", "nodename nor servname", "no such host", "name resolution", "host not found", "unknown host", "non-existent domain")):
        return "HOST UNKNOWN", detail
    if any(x in lower for x in ("badhostkey", "host key", "known_hosts", "server not found in known_hosts")):
        return "SSH HOST KEY ERROR", detail
    if any(x in lower for x in ("connection refused", "actively refused")):
        return "SSH PORT REFUSED", detail
    if any(x in lower for x in ("timeout", "timed out", "tcp connection", "network is unreachable", "no route to host", "socket")):
        return "SSH CONNECTION FAILED", detail
    if "ssh autodetection" in lower:
        return "DEVICE TYPE UNKNOWN", detail
    return "SSH CONNECTION FAILED", detail


def extract_cli_prompt(text: str) -> str | None:
    """Return the *final* standalone prompt, never an intermediate output line."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n").split("\n")
    if not lines:
        return None
    candidate = lines[-1].strip()
    return candidate if PROMPT_PATTERN.fullmatch(candidate) else None


def cli_mode(prompt: str) -> str:
    """A Cisco-like mode hint. No vendor configuration commands are injected."""
    return "config" if CONFIG_MODES.search(prompt) else "exec"


@dataclass
class CliModeTracker:
    """Observe real prompts after every command and validate common transitions.

    The device is authoritative: NetCmd never guesses or sends an extra end command.
    Unknown vendor prompts are permitted as long as a complete prompt is read.
    """
    current_prompt: str
    device_type: str

    def observe(self, command: str, response: str) -> str:
        next_prompt = extract_cli_prompt(response)
        if not next_prompt:
            raise CommandFailure("No complete CLI prompt after command; command outcome unknown")
        previous_mode = cli_mode(self.current_prompt)
        next_mode = cli_mode(next_prompt)
        cmd = command.strip().lower()
        # Validate mode transitions for the Cisco/Arista CLI family only.
        # Other vendors keep prompt synchronization without Cisco semantics.
        supported = self.device_type.startswith(("cisco_", "arista_"))
        if supported and CONFIG_ENTER.fullmatch(cmd) and next_mode != "config":
            raise CommandFailure(f"Configuration mode not entered (prompt: {next_prompt})")
        if supported and CONFIG_LEAVE.fullmatch(cmd) and previous_mode == "config" and next_mode == "config":
            raise CommandFailure(f"Configuration mode not exited (prompt: {next_prompt})")
        self.current_prompt = next_prompt
        return next_prompt


def session_transcript(prompt: str, command: str, response: str) -> str:
    """Use the prompt captured before the command; omit the resulting prompt.

    Never add an extra prompt line when the device switches to a new submode.
    """
    cleaned = response.replace("\r\n", "\n").replace("\r", "\n")
    lines = cleaned.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    command_text = command.strip()
    if lines:
        first = lines[0].strip()
        if first == command_text or (prompt and first in (prompt.strip() + command_text, prompt.strip() + " " + command_text)):
            lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    # The final prompt may differ from the prompt before the command!
    if lines and extract_cli_prompt(lines[-1]):
        lines.pop()
    remainder = "\n".join(lines).strip("\n")
    title = f"{prompt.strip() if prompt else ''}{command_text}"
    return title + "\n" + (remainder + "\n" if remainder else "")


def channel_read_until(connection: Any, pattern: str, timeout: float, *, quiet_prompt: bool = False) -> str:
    """Wait until the last line is a CLI prompt (or a specific interactive regex)."""
    compiled = None if quiet_prompt else re.compile(pattern, re.I | re.M)
    collected = ""
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        chunk = connection.read_channel()
        if chunk:
            collected += chunk
            if quiet_prompt:
                if extract_cli_prompt(collected):
                    return collected
            elif compiled and compiled.search(collected):
                return collected
        else:
            time.sleep(0.15)
    kind = "complete CLI prompt" if quiet_prompt else f"expected pattern {pattern!r}"
    raise CommandFailure(f"Timeout after {timeout:g}s waiting for {kind}; last output: {collected[-250:]!r}")


def wait_disconnect(connection: Any, timeout: float) -> str:
    """Require observable SSH closure, never assume it merely from missing output."""
    end = time.monotonic() + timeout
    transcript = ""
    while time.monotonic() < end:
        try:
            if hasattr(connection, "is_alive") and not connection.is_alive():
                return transcript
            transcript += connection.read_channel()
            time.sleep(1)
        except (OSError, EOFError, ConnectionError):
            return transcript
        except Exception as exc:
            # Netmiko often raises a transport-specific exception on reboot.
            if any(x in str(exc).lower() for x in ("closed", "disconnect", "socket", "eof", "reset", "not active")):
                return transcript
            raise
    raise CommandFailure(f"No SSH disconnect observed within {timeout:g}s")


def reconnect_until(host: str, dtype: str, username: str, password: str, args: Any, timeout: float):
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            connection, _ = detect_and_connect(host, dtype, username, password, args)
            return connection
        except Exception as exc:
            last_error = str(exc)
            time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise CommandFailure(f"Reconnect timeout after {timeout:g}s: {last_error}")


def run_command(connection: Any, step: CommandStep, timeout: float, host: str, dtype: str,
                username: str, password: str, args: Any) -> tuple[str, Any]:
    """Send exactly once. Expect/send pairs are ordered; reconnect never replays."""
    transcript = ""
    # Read initial stable prompt for non-interactive commands; prompt may differ
    # after entering config mode, so use end-of-line CLI prompt matching.
    connection.write_channel(step.command + "\n")
    if step.interactions:
        for expected, answer in step.interactions:
            transcript += channel_read_until(connection, expected, timeout)
            payload = "\n" if answer.lower() == "[enter]" else answer + "\n"
            connection.write_channel(payload)
    if step.wait_disconnect is not None:
        transcript += wait_disconnect(connection, step.wait_disconnect)
        try:
            connection.disconnect()
        except Exception:
            pass
        connection = reconnect_until(host, dtype, username, password, args, step.reconnect or 0)
        return transcript, connection
    # Complete response, even after final @send; do not run next command early.
    transcript += channel_read_until(connection, PROMPT_PATTERN.pattern, timeout, quiet_prompt=True)
    return transcript, connection


def classify_command(command: str, output: str) -> str | None:
    if ERROR_PATTERN.search(output):
        return "CLI error reported"
    if command.strip().lower() in {"wr", "write memory"} and not re.search(r"(?im)^\s*\[OK\]\s*$", output):
        return "write memory did not return [OK]"
    return None


def device_worker(job: DeviceJob, args: Any, logger: logging.Logger, outpath: Path,
                  username: str, password: str) -> DeviceResult:
    total = sum(isinstance(s, CommandStep) for s in job.steps)
    result = DeviceResult(host=job.host, total=total, output_path=str(outpath))
    output_chunks: list[str] = []
    connection = None
    try:
        try:
            connection, dtype = detect_and_connect(job.host, job.device_type, username, password, args)
        except Exception as exc:
            result.connection_error, result.connection_details = classify_connection_error(exc)
            raise
        logger.info("%s | Connected | device_type=%s | port=%d", job.host, dtype, args.port)
        if args.test_connection:
            result.status = "PASS"
            result.reason = "SSH authentication successful"
            logger.info("%s | TEST CONNECTION | PASS", job.host)
            return result
        if args.mode == "config":
            connection.config_mode()
        tracker = CliModeTracker(connection.find_prompt().strip(), dtype)
        for step in job.steps:
            if isinstance(step, WaitStep):
                logger.info("%s | Wait %.1fs", job.host, step.seconds)
                time.sleep(step.seconds)
                continue
            timeout = step.timeout or args.timeout
            logger.info("%s | Executing line=%d | %s", job.host, step.line, redact_command(step.command))
            start = time.perf_counter()
            try:
                # Refresh from the live device; keeps state consistent with the channel.
                prompt = connection.find_prompt().strip()
                tracker.current_prompt = prompt
                response, connection = run_command(connection, step, timeout, job.host, dtype, username, password, args)
                output_chunks.append(session_transcript(prompt, step.command, response).rstrip("\n") + "\n\n")
                reason = classify_command(step.command, response)
                if reason:
                    raise CommandFailure(reason)
                if step.wait_disconnect is not None:
                    tracker.current_prompt = connection.find_prompt().strip()
                else:
                    tracker.observe(step.command, response)
                result.passed += 1
                logger.info("%s | PASS | %s | %.2fs", job.host, redact_command(step.command), time.perf_counter()-start)
            except Exception as exc:
                result.failed += 1
                result.reason = f"{redact_command(step.command)}: {exc}"
                logger.error("%s | FAILED | %s | %s", job.host, redact_command(step.command), exc)
                # After SSH transport timeout the command's outcome is unknown;
                # continuing risks issuing commands into a desynchronized channel.
                unsafe = "timeout" in str(exc).lower() or "disconnect" in str(exc).lower() or "ssh" in str(exc).lower()
                if args.on_error == "stop" or unsafe:
                    break
        result.skipped = total - result.passed - result.failed
        result.status = "PASS" if result.failed == 0 and result.skipped == 0 else ("PARTIAL" if result.passed else "FAILED")
    except Exception as exc:
        result.reason = str(exc)
        result.skipped = total - result.passed - result.failed
        result.status = "PARTIAL" if result.passed else "FAILED"
        logger.error("%s | DEVICE FAILED | %s", job.host, exc)
    finally:
        if connection is not None:
            try:
                if args.mode == "config" and connection.is_alive():
                    connection.exit_config_mode()
                connection.disconnect()
            except Exception:
                pass
        # Never generate a blank .txt on authentication/SSH failure or test mode.
        # Keep all actual CLI evidence, including CLI error responses.
        if output_chunks and not args.test_connection:
            try:
                outpath.write_text("".join(output_chunks), encoding="utf-8")
            except OSError as exc:
                result.status = "FAILED"
                result.reason = f"Cannot write output: {exc}"
                logger.error("%s | OUTPUT FAILED | %s", job.host, exc)
        logger.info("%s | RESULT | %s | pass=%d failed=%d skipped=%d | %s", job.host, result.status,
                    result.passed, result.failed, result.skipped, result.reason)
    return result


def duration_string(seconds: float) -> str:
    n = round(seconds)
    h, rem = divmod(n, 3600)
    m, s = divmod(rem, 60)
    return (f"{h} hours " if h else "") + (f"{m} minutes " if m else "") + f"{s} seconds"


def interactive_options() -> tuple[list[str], str]:
    """Prompt for the basic workflow; keep password out of shell history."""
    print("NET COMMAND RUNNER\n\nInteractive Mode\n")
    while True:
        source = input("Host / host file / multifile: ").strip().strip('"')
        if source:
            break
    if source.lower().startswith("mf:"):
        selected = ["-mf", source[3:].strip()]
    elif Path(source).is_file() or source.lower().endswith((".txt", ".csv")):
        # A host-file path is selected by default; use mf:PATH for Multifile.
        selected = ["-hf", source]
    else:
        selected = ["-ho", source]
    cmd = "" if selected[0] == "-mf" else input("Command file: ").strip().strip('"')
    while selected[0] != "-mf" and not cmd:
        cmd = input("Command file (required): ").strip().strip('"')
    if cmd:
        selected.extend(["-cmd", cmd])
    username = input("User: ").strip()
    while not username:
        username = input("User (required): ").strip()
    password = getpass.getpass("Password: ")
    suffix = input("Suffix (optional): ").strip()
    selected.extend(["-u", username])
    if suffix:
        selected.extend(["--suffix", suffix])
    return selected, password


def overall_status(results: list[DeviceResult]) -> tuple[str, str]:
    if all(r.status == "PASS" for r in results):
        return "All Success", "green"
    if any(r.failed or (r.status == "PARTIAL" and not r.connection_error) for r in results):
        return "Some Commands Failed", "red"
    return "Some Devices Failed", "red"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    supplied = sys.argv[1:] if argv is None else argv
    wizard_password = None
    wizard_mode = not supplied
    if wizard_mode:
        if not sys.stdin.isatty():
            parser.error("interactive mode requires a terminal")
        supplied, wizard_password = interactive_options()
    args = parser.parse_args(supplied)
    if wizard_password is not None:
        args.password = wizard_password
    if args.multifile and args.command_file:
        parser.error("-mf/--multifile cannot be combined with -cf/-cmd")
    if not args.multifile and not args.command_file and not args.test_connection:
        parser.error("-ho/-hf requires -cf/-cmd unless --test-connection is used")
    if not (1 <= args.port <= 65535):
        parser.error("--port must be within 1..65535")
    for path_opt in (args.ssh_config, args.known_hosts):
        if path_opt and not Path(path_opt).expanduser().is_file():
            parser.error(f"SSH configuration file not found: {path_opt}")
    if args.retries < 0:
        parser.error("--retries must be >= 0")
    try:
        jobs = load_multifile(args.multifile, require_commands=not args.test_connection) if args.multifile else load_standard(args.host, args.host_file, args.command_file, require_commands=not args.test_connection)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        names = [result_filename(j.host, args, stamp) for j in jobs]
        if len({n.lower() for n in names}) != len(names):
            raise InputError("Output filename collision (case-insensitive)")
    except InputError as exc:
        parser.error(str(exc))
    mode = "MULTIFILE" if args.multifile else "STANDARD"
    commands_count = sum(isinstance(s, CommandStep) for j in jobs for s in j.steps)
    if not wizard_mode:
        print("NET COMMAND RUNNER\n")
    print(f"Mode       : {mode}\nDevices    : {len(jobs)}\nWorkers    : {args.workers}")
    disruptive = any(isinstance(step, CommandStep) and DISRUPTIVE.search(step.command)
                     for job in jobs for step in job.steps)
    if args.dry_run:
        print("\nDRY RUN — no SSH connections; no output files will be generated.\n")
        for job, name in zip(jobs, names):
            print(f"HostName:{job.host} | DeviceType:{job.device_type or args.device_type or 'autodetect'} | {name}")
            for step in job.steps:
                if isinstance(step, WaitStep):
                    print(f"  @wait {step.seconds:g}")
                else:
                    print(f"  {redact_command(step.command)}")
                    if step.timeout: print(f"    timeout={step.timeout:g}s")
                    for expect, answer in step.interactions:
                        print(f"    expect={expect!r} send={'[REDACTED]' if SENSITIVE.search(answer) else answer!r}")
                    if step.wait_disconnect: print(f"    wait-disconnect={step.wait_disconnect:g}s reconnect={step.reconnect:g}s")
        return 0
    if disruptive and not args.yes:
        if not sys.stdin.isatty():
            parser.error("Disruptive commands detected; rerun with --yes after reviewing --dry-run")
        response = input("\nPotentially disruptive commands detected. Proceed? [y/N]: ").strip().lower()
        if response not in ("y", "yes"):
            print("Cancelled.")
            return 2
    if args.user is None:
        args.user = input("SSH username: ").strip()
    if not args.user:
        parser.error("SSH username cannot be empty")
    if args.password is None:
        args.password = getpass.getpass("SSH password: ")
    try:
        from rich.console import Console
        from rich.progress import Progress, SpinnerColumn, BarColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
        import netmiko  # noqa: F401
    except ImportError as exc:
        parser.error(f"Missing dependency: {exc}. Run: py -m pip install -r requirements.txt")
    root = Path(args.output)
    result_dir, log_dir = root / "Results", root / "Logs"
    try:
        existing = [] if args.test_connection else [result_dir / n for n in names if (result_dir / n).exists()]
        if existing and not args.overwrite:
            raise InputError(f"Output already exists: {existing[0]}; use --overwrite, --timestamp or another -o")
        if not args.test_connection:
            result_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
    except (InputError, OSError) as exc:
        parser.error(str(exc))
    # Avoid accidental overwrite of logs from two runs launched in one second.
    logsuffix = stamp
    suffix_no = 1
    while (log_dir / f"NetCmd_{logsuffix}.log").exists() or (log_dir / f"Summary_{logsuffix}.log").exists():
        logsuffix = f"{stamp}_{suffix_no}"
        suffix_no += 1
    logger = setup_logger(log_dir / f"NetCmd_{logsuffix}.log")
    started_wall = datetime.now()
    started_perf = time.perf_counter()
    print(f"Start Time : {started_wall:%Y-%m-%d %H:%M:%S}\n")
    logger.info("Started | mode=%s devices=%d commands=%d workers=%d", mode, len(jobs), commands_count, args.workers)
    results: list[DeviceResult] = []
    console = Console()
    progress = Progress(SpinnerColumn(), TextColumn("Executing commands"), BarColumn(), TaskProgressColumn(),
                        TextColumn("{task.completed}/{task.total}"), TimeElapsedColumn(), console=console)
    with progress:
        task = progress.add_task("Executing commands", total=len(jobs))
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            submitted = {executor.submit(device_worker, job, args, logger, result_dir / name, args.user, args.password): job
                         for job, name in zip(jobs, names)}
            for future in concurrent.futures.as_completed(submitted):
                job = submitted[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = DeviceResult(job.host, sum(isinstance(s, CommandStep) for s in job.steps),
                                          status="FAILED", reason=f"Unhandled worker failure: {exc}")
                    logger.exception("%s | Unhandled worker error", job.host)
                results.append(result)
                if result.connection_error:
                    progress.console.print(f"[red]{result.host} | {result.connection_error}[/red]")
                elif args.test_connection:
                    progress.console.print(f"[green]{result.host} | SSH SUCCESS[/green]")
                elif args.verbose:
                    progress.console.print(f"{result.host:<24} {result.status:<8} {result.passed}/{result.total}"
                                           + (f" | {result.reason[:120]}" if result.reason else ""))
                progress.advance(task)
    ended_wall = datetime.now()
    elapsed = time.perf_counter() - started_perf
    results_by_host = {r.host.casefold(): r for r in results}
    ordered = [results_by_host[j.host.casefold()] for j in jobs]
    summary_path = log_dir / f"Summary_{logsuffix}.log"
    with summary_path.open("w", encoding="utf-8") as file:
        file.write("NET COMMAND RUNNER — EXECUTION SUMMARY\n")
        file.write(f"Mode: {mode}\nStart: {started_wall:%Y-%m-%d %H:%M:%S}\n")
        file.write(f"End: {ended_wall:%Y-%m-%d %H:%M:%S}\nDuration: {duration_string(elapsed)}\n")
        file.write(f"Devices: {len(jobs)} | Workers: {args.workers}\n\n")
        file.write(f"{'DEVICE':<28} {'PASS':>5} {'FAIL':>5} {'SKIP':>5} STATUS\n")
        for r in ordered:
            file.write(f"{r.host:<28} {r.passed:>5} {r.failed:>5} {r.skipped:>5} {r.status}\n")
        file.write("\nFAILED / PARTIAL DEVICES\n")
        for r in ordered:
            if r.status != "PASS":
                file.write(f"{r.host} | {r.connection_error or r.status} | {r.reason}\n")
        file.write("\nCOUNTS\n")
        for status in ("PASS", "PARTIAL", "FAILED"):
            file.write(f"{status}: {sum(r.status == status for r in ordered)}\n")
    logger.info("Finished | devices=%d pass=%d partial=%d failed=%d elapsed=%.2fs",
                len(jobs), *(sum(r.status == s for r in ordered) for s in ("PASS", "PARTIAL", "FAILED")), elapsed)
    print(f"\nCompleted  : {len(jobs)} devices\nDone in    : {duration_string(elapsed)}")
    print(f"End Time   : {ended_wall:%Y-%m-%d %H:%M:%S}")
    final_label, final_color = overall_status(ordered)
    console.print(f"Status     : [{final_color}]{final_label}[/{final_color}]\n")
    if not args.test_connection:
        print(f"Results    : {result_dir}/")
    print(f"Logs       : {log_dir}/")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    return 1 if any(r.status != "PASS" for r in ordered) else 0


if __name__ == "__main__":
    sys.exit(main())