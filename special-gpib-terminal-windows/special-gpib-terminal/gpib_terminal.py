"""Standalone, transparent VISA/GPIB terminal for the six lab instruments.

The program intentionally keeps the implementation in one file.  A VISA resource
selects the device; only the SCPI/TSP message after that resource is placed on the
instrument bus.  Every operation opens and closes its own VISA session and is
recorded in an append-only JSON Lines log.
"""

from __future__ import print_function

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


LAB_INSTRUMENTS = {
    "GPIB0::6::INSTR": {"model": "2182A", "serial": "1340129"},
    "GPIB0::7::INSTR": {"model": "2182A", "serial": "4510267"},
    "GPIB0::9::INSTR": {"model": "6221", "serial": "4533811"},
    "GPIB0::10::INSTR": {"model": "6221", "serial": "4581062"},
    "GPIB0::25::INSTR": {"model": "2450", "serial": "04584128"},
    "GPIB0::26::INSTR": {"model": "2450", "serial": "04464720"},
}

WRITE_UNLOCK_PHRASE = "I_UNDERSTAND_WRITES_CAN_CHANGE_INSTRUMENTS"
ACTIVE_QUERY_CONFIRMATION = "SEND ACTIVE_QUERY"
NORMAL_WRITE_CONFIRMATION = "SEND"
HIGH_RISK_WRITE_CONFIRMATION = "SEND HIGH_RISK"


def utc_now_text():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_device_operation(rest):
    """Return ``(resource, message)`` while preserving the message text."""
    parts = rest.strip().split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError("Expected: <VISA-resource> <instrument-message>")
    return parts[0].upper(), parts[1].strip()


def validate_single_message(message):
    if not message:
        raise ValueError("The instrument message is empty.")
    if any(character in message for character in ("\r", "\n", ";")):
        raise ValueError(
            "Only one instrument message is allowed per terminal line; "
            "newlines and semicolons are blocked."
        )


def is_query_message(message):
    normalized = message.strip().lower()
    return "?" in normalized or normalized.startswith("print(")


def is_active_query(message):
    """Identify queries that trigger/consume a reading or consume a status queue."""
    normalized = re.sub(r"\s+", "", message.upper())
    active_markers = (
        "READ?",
        "MEAS?",
        "MEASURE?",
        "SENS:DATA:FRESH?",
        "SENSE:DATA:FRESH?",
        "SYST:ERR?",
        "SYSTEM:ERROR?",
        "*ESR?",
        "EVENT?",
    )
    return any(marker in normalized for marker in active_markers)


def is_high_risk_write(message):
    """Return True for writes that can energize, source, trigger, or reset."""
    normalized = re.sub(r"\s+", " ", message.strip().upper())

    if re.match(r"^OUTP(?:UT)?(?:[: ](?:STAT(?:E)?))?\s+(?:ON|1)\b", normalized):
        return True

    high_risk_prefixes = (
        "*RST",
        "*CLS",
        "*TRG",
        "INIT",
        "TRIG",
        "ABOR",
        "SYST:PRES",
        "SYSTEM:PRESET",
        "SOUR:CURR",
        "SOURCE:CURRENT",
        "SOUR:VOLT",
        "SOURCE:VOLTAGE",
        "SOUR:DELT",
        "SOURCE:DELTA",
    )
    if normalized.startswith(high_risk_prefixes):
        return True

    # TSP state changes are assignments or function calls.  Query-only print(...)
    # messages never reach this function because WRITE rejects query messages.
    if "=" in normalized or normalized.startswith(("SMU.", "RESET(", "TRIGGER.")):
        return True

    return False


class JsonlSessionLog(object):
    def __init__(self, log_directory):
        directory = Path(log_directory).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        self.path = directory / (stamp + "-gpib-terminal.jsonl")
        # Exclusive creation prevents accidental overwrite even if names collide.
        with self.path.open("x", encoding="utf-8") as handle:
            handle.write("")

    def record(self, event, **fields):
        entry = {"utc": utc_now_text(), "event": event}
        entry.update(fields)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


class PyVisaBackend(object):
    """Very small PyVISA adapter: one open/query-or-write/close per command."""

    def _new_resource_manager(self):
        try:
            import pyvisa
        except ImportError as error:
            raise RuntimeError(
                "PyVISA is not installed in this Python environment. "
                "Run the terminal with the existing instrument-probe .venv."
            ) from error
        return pyvisa.ResourceManager()

    def list_resources(self):
        manager = self._new_resource_manager()
        try:
            return tuple(manager.list_resources())
        finally:
            manager.close()

    def query(self, resource_name, message, timeout_ms):
        manager = self._new_resource_manager()
        instrument = None
        try:
            instrument = manager.open_resource(resource_name)
            instrument.timeout = timeout_ms
            # Do not set read_termination or write_termination for GPIB.  NI-VISA
            # uses the GPIB EOI handshake, matching the lab's real communication.
            return instrument.query(message)
        finally:
            if instrument is not None:
                instrument.close()
            manager.close()

    def write(self, resource_name, message, timeout_ms):
        manager = self._new_resource_manager()
        instrument = None
        try:
            instrument = manager.open_resource(resource_name)
            instrument.timeout = timeout_ms
            return instrument.write(message)
        finally:
            if instrument is not None:
                instrument.close()
            manager.close()


class GpibTerminal(object):
    def __init__(
        self,
        backend,
        session_log,
        timeout_ms=3000,
        input_fn=input,
        output_fn=print,
    ):
        self.backend = backend
        self.log = session_log
        self.timeout_ms = int(timeout_ms)
        self.input_fn = input_fn
        self.output_fn = output_fn
        self.writes_unlocked = False

    def emit(self, text=""):
        self.output_fn(text)

    def show_banner(self):
        self.emit("Special GPIB Terminal - real PyVISA / NI-VISA communication")
        self.emit("WRITE is LOCKED by default. QUERY sends exactly one message.")
        self.emit("Type HELP for syntax. Type MAP for the six confirmed addresses.")
        self.emit("Log: " + str(self.log.path))

    def show_help(self):
        self.emit("Local commands (no instrument I/O):")
        self.emit("  HELP")
        self.emit("  MAP")
        self.emit("  STATUS")
        self.emit("  TIMEOUT <milliseconds>")
        self.emit("  CALC-R <voltage_V> <current_A>")
        self.emit("  LOCK-WRITES")
        self.emit("  UNLOCK-WRITES " + WRITE_UNLOCK_PHRASE)
        self.emit("  EXIT")
        self.emit("VISA/GPIB commands:")
        self.emit("  LIST")
        self.emit("  QUERY <resource> <SCPI-or-TSP-query>   (alias: Q)")
        self.emit("  WRITE <resource> <SCPI-or-TSP-write>  (alias: W)")
        self.emit("Example:")
        self.emit("  QUERY GPIB0::9::INSTR *IDN?")

    def show_map(self):
        self.emit("Confirmed laboratory address map:")
        for resource, identity in LAB_INSTRUMENTS.items():
            self.emit(
                "  {0:<19} MODEL {1:<5}  S/N {2}".format(
                    resource, identity["model"], identity["serial"]
                )
            )

    def show_status(self):
        self.emit("Write gate: " + ("UNLOCKED" if self.writes_unlocked else "LOCKED"))
        self.emit("Timeout: {0} ms".format(self.timeout_ms))
        self.emit("Allowed device addresses: {0}".format(len(LAB_INSTRUMENTS)))
        self.emit("Log: " + str(self.log.path))

    def confirm(self, expected, explanation):
        self.emit(explanation)
        answer = self.input_fn("Confirmation> ").strip()
        accepted = answer == expected
        self.log.record(
            "confirmation",
            expected=expected,
            accepted=accepted,
            received=answer,
        )
        if not accepted:
            self.emit("CANCELLED (expected exactly: {0})".format(expected))
        return accepted

    def require_known_resource(self, resource):
        if resource not in LAB_INSTRUMENTS:
            raise ValueError(
                "Unconfirmed resource is blocked: {0}. Type MAP for allowed addresses.".format(
                    resource
                )
            )

    def run_list(self):
        started = time.perf_counter()
        try:
            resources = self.backend.list_resources()
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
            self.emit("VISA resources (enumeration only; no instrument message sent):")
            if resources:
                for resource in resources:
                    self.emit("  " + str(resource))
            else:
                self.emit("  (none)")
            self.log.record(
                "list_resources", ok=True, resources=list(resources), elapsed_ms=elapsed_ms
            )
        except Exception as error:
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
            self.emit("ERROR: {0}: {1}".format(type(error).__name__, error))
            self.log.record(
                "list_resources",
                ok=False,
                error="{0}: {1}".format(type(error).__name__, error),
                elapsed_ms=elapsed_ms,
            )

    def run_query(self, resource, message):
        self.require_known_resource(resource)
        validate_single_message(message)
        if not is_query_message(message):
            raise ValueError(
                "QUERY requires a '?' command or a 2450 TSP print(...). "
                "State-changing messages belong to WRITE."
            )

        active = is_active_query(message)
        if active and not self.confirm(
            ACTIVE_QUERY_CONFIRMATION,
            "ACTIVE QUERY: this may trigger/consume a reading or consume a status queue.\n"
            "Type exactly: " + ACTIVE_QUERY_CONFIRMATION,
        ):
            return

        identity = LAB_INSTRUMENTS[resource]
        self.emit(
            "OPEN  {0}  (MODEL {1}, expected S/N {2})".format(
                resource, identity["model"], identity["serial"]
            )
        )
        self.emit("TX -> " + message)
        started = time.perf_counter()
        try:
            response = self.backend.query(resource, message, self.timeout_ms)
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
            response_text = str(response).rstrip("\r\n")
            self.emit("RX <- " + response_text)
            self.emit("CLOSE {0}  ({1} ms)".format(resource, elapsed_ms))
            self.log.record(
                "query",
                ok=True,
                resource=resource,
                message=message,
                response=response_text,
                active_query=active,
                timeout_ms=self.timeout_ms,
                elapsed_ms=elapsed_ms,
            )
        except Exception as error:
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
            self.emit("ERROR: {0}: {1}".format(type(error).__name__, error))
            self.emit("CLOSE {0}  ({1} ms)".format(resource, elapsed_ms))
            self.log.record(
                "query",
                ok=False,
                resource=resource,
                message=message,
                active_query=active,
                timeout_ms=self.timeout_ms,
                error="{0}: {1}".format(type(error).__name__, error),
                elapsed_ms=elapsed_ms,
            )

    def run_write(self, resource, message):
        self.require_known_resource(resource)
        validate_single_message(message)
        if is_query_message(message):
            raise ValueError("A query/print message must use QUERY, not WRITE.")
        if not self.writes_unlocked:
            raise ValueError(
                "WRITE is locked. Use the explicit UNLOCK-WRITES command only after "
                "the wiring, limits, and intended command have been reviewed."
            )

        high_risk = is_high_risk_write(message)
        expected = (
            HIGH_RISK_WRITE_CONFIRMATION if high_risk else NORMAL_WRITE_CONFIRMATION
        )
        risk_text = "HIGH-RISK WRITE" if high_risk else "WRITE"
        if not self.confirm(
            expected,
            "{0}: the following exact message will change instrument state:\n"
            "  {1}  {2}\nType exactly: {3}".format(
                risk_text, resource, message, expected
            ),
        ):
            return

        identity = LAB_INSTRUMENTS[resource]
        self.emit(
            "OPEN  {0}  (MODEL {1}, expected S/N {2})".format(
                resource, identity["model"], identity["serial"]
            )
        )
        self.emit("TX -> " + message)
        started = time.perf_counter()
        try:
            count = self.backend.write(resource, message, self.timeout_ms)
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
            self.emit("WRITE accepted by VISA ({0} bytes reported).".format(count))
            self.emit("CLOSE {0}  ({1} ms)".format(resource, elapsed_ms))
            self.log.record(
                "write",
                ok=True,
                resource=resource,
                message=message,
                high_risk=high_risk,
                timeout_ms=self.timeout_ms,
                visa_write_count=count,
                elapsed_ms=elapsed_ms,
            )
        except Exception as error:
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
            self.emit("ERROR: {0}: {1}".format(type(error).__name__, error))
            self.emit("CLOSE {0}  ({1} ms)".format(resource, elapsed_ms))
            self.log.record(
                "write",
                ok=False,
                resource=resource,
                message=message,
                high_risk=high_risk,
                timeout_ms=self.timeout_ms,
                error="{0}: {1}".format(type(error).__name__, error),
                elapsed_ms=elapsed_ms,
            )

    def calculate_resistance(self, rest):
        parts = rest.split()
        if len(parts) != 2:
            raise ValueError("Expected: CALC-R <voltage_V> <current_A>")
        voltage = float(parts[0])
        current = float(parts[1])
        if current == 0:
            raise ValueError("Current cannot be zero when calculating R = V / I.")
        resistance = voltage / current
        self.emit("LOCAL ONLY: R = V / I = {0:.12g} ohm".format(resistance))
        self.log.record(
            "calculate_resistance",
            voltage_v=voltage,
            current_a=current,
            resistance_ohm=resistance,
        )

    def execute_line(self, line):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return True

        operation, separator, rest = stripped.partition(" ")
        operation = operation.upper()
        rest = rest.strip() if separator else ""

        try:
            if operation in ("EXIT", "QUIT"):
                self.writes_unlocked = False
                self.log.record("session_end", reason=operation.lower())
                self.emit("Write gate locked. Session ended.")
                return False
            if operation == "HELP":
                self.show_help()
            elif operation == "MAP":
                self.show_map()
            elif operation == "STATUS":
                self.show_status()
            elif operation == "LIST":
                if rest:
                    raise ValueError("LIST takes no arguments.")
                self.run_list()
            elif operation in ("QUERY", "Q"):
                resource, message = parse_device_operation(rest)
                self.run_query(resource, message)
            elif operation in ("WRITE", "W"):
                resource, message = parse_device_operation(rest)
                self.run_write(resource, message)
            elif operation == "UNLOCK-WRITES":
                if rest != WRITE_UNLOCK_PHRASE:
                    raise ValueError(
                        "Unlock phrase mismatch. Type HELP and copy the exact phrase."
                    )
                self.writes_unlocked = True
                self.log.record("write_gate", state="unlocked")
                self.emit("Write gate: UNLOCKED. Every WRITE still needs confirmation.")
            elif operation == "LOCK-WRITES":
                if rest:
                    raise ValueError("LOCK-WRITES takes no arguments.")
                self.writes_unlocked = False
                self.log.record("write_gate", state="locked")
                self.emit("Write gate: LOCKED.")
            elif operation == "TIMEOUT":
                value = int(rest)
                if not 100 <= value <= 60000:
                    raise ValueError("TIMEOUT must be between 100 and 60000 ms.")
                self.timeout_ms = value
                self.log.record("timeout", timeout_ms=value)
                self.emit("Timeout set to {0} ms (host-side setting only).".format(value))
            elif operation == "CALC-R":
                self.calculate_resistance(rest)
            else:
                raise ValueError("Unknown terminal command: {0}. Type HELP.".format(operation))
        except (ValueError, RuntimeError) as error:
            self.emit("BLOCKED: " + str(error))
            self.log.record(
                "blocked_input", operation=operation, input=stripped, reason=str(error)
            )
        return True

    def run(self):
        self.log.record(
            "session_start",
            python=sys.version.split()[0],
            platform=sys.platform,
            process_id=os.getpid(),
            timeout_ms=self.timeout_ms,
            allowed_resources=list(LAB_INSTRUMENTS),
        )
        self.show_banner()
        while True:
            try:
                line = self.input_fn("GPIB> ")
            except (EOFError, KeyboardInterrupt):
                self.emit("")
                self.writes_unlocked = False
                self.log.record("session_end", reason="interrupt_or_eof")
                self.emit("Write gate locked. Session ended.")
                break
            if not self.execute_line(line):
                break


def build_argument_parser():
    parser = argparse.ArgumentParser(
        description="Standalone terminal for direct PyVISA/GPIB messages."
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=3000,
        help="Host-side VISA timeout in milliseconds (default: 3000).",
    )
    parser.add_argument(
        "--log-dir",
        default=str(Path(__file__).resolve().parent / "terminal_logs"),
        help="Directory for append-only JSONL session logs.",
    )
    return parser


def main(argv=None):
    arguments = build_argument_parser().parse_args(argv)
    if not 100 <= arguments.timeout_ms <= 60000:
        raise SystemExit("--timeout-ms must be between 100 and 60000")
    session_log = JsonlSessionLog(arguments.log_dir)
    terminal = GpibTerminal(
        backend=PyVisaBackend(),
        session_log=session_log,
        timeout_ms=arguments.timeout_ms,
    )
    terminal.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
