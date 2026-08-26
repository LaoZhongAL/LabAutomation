"""Safety gate for every message sent to a real instrument."""

from __future__ import annotations

from .catalog import Profile


class UnsafeCommandError(ValueError):
    pass


# These queries either initiate acquisition, consume queue/event state, or can
# alter the running trigger model.  They remain blocked even if accidentally
# added to a future catalog.
BLOCKED_EXACT = {"READ?", "FETCH?", "FETC?", "*ESR?", "*TST?", "SYST:KEY?"}


def _is_state_consuming_or_acquiring(upper: str) -> bool:
    compact = upper.replace(" ", "")
    return (
        compact in BLOCKED_EXACT
        or compact.startswith("MEAS")
        or compact.startswith("READ?")
        or compact.startswith("FETCH?")
        or compact.startswith("FETC?")
        or ":DATA:FRESH?" in compact
        or compact.startswith("TRACE:DATA?")
        or compact.startswith("TRAC:DATA?")
        or compact.startswith("SYST:ERR")
        or compact.startswith("STAT:QUEUE")
        or compact.endswith(":EVENT?")
        or compact.endswith(":EVEN?")
        or compact.endswith(":NEXT?")
    )


def validate_query(profile: Profile, command: str) -> None:
    if not isinstance(command, str) or not command.strip():
        raise UnsafeCommandError("empty command")
    if command != command.strip():
        raise UnsafeCommandError("leading/trailing whitespace is not allowed")
    if any(char in command for char in ("\n", "\r", ";")):
        raise UnsafeCommandError("multi-command messages are forbidden")

    allowlist = {item.command for item in profile.queries}
    if command not in allowlist:
        raise UnsafeCommandError(f"command is not in the exact allowlist: {command!r}")

    upper = command.upper()
    if _is_state_consuming_or_acquiring(upper):
        raise UnsafeCommandError(f"state-consuming or acquisition query is forbidden: {command!r}")

    if profile.command_set == "SCPI":
        if "?" not in command:
            raise UnsafeCommandError("SCPI message is not a query")
    elif profile.command_set == "TSP":
        if command != "*IDN?" and not (command.startswith("print(") and command.endswith(")")):
            raise UnsafeCommandError("TSP allowlist only permits exact print(...) expressions")
        if "=" in command:
            raise UnsafeCommandError("TSP assignment is forbidden")
    else:
        raise UnsafeCommandError(f"unknown command set: {profile.command_set}")
