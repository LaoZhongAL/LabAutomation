from __future__ import annotations

import importlib.metadata
import locale
import platform
import struct
import sys
import time
from datetime import datetime, timezone

from . import __version__
from .catalog import Profile, identity_matches, queries_for
from .transports import QueryTransport


def host_environment() -> dict[str, object]:
    packages: dict[str, str | None] = {"readonly-instrument-probe": __version__}
    for name in ("pyvisa", "pyvisa-py", "pyvisa-sim"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "os": platform.platform(),
        "os_system": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "python_bits": struct.calcsize("P") * 8,
        "python": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "locale": locale.getlocale(),
        "timezone": time.tzname,
        "packages": packages,
    }


def collect(profile: Profile, transport: QueryTransport, scope: str) -> dict[str, object]:
    started = datetime.now(timezone.utc)
    results: dict[str, dict[str, object]] = {}
    transcript: list[dict[str, object]] = []
    stopped_after_error = False
    stopped_after_identity_mismatch = False

    for item in queries_for(profile, scope):
        t0 = time.monotonic()
        entry: dict[str, object] = {
            "name": item.name,
            "command": item.command,
            "group": item.group,
        }
        try:
            value = transport.query(item.command)
            entry["ok"] = True
            entry["response"] = value
            results[item.name] = {"value": value, "group": item.group, "note": item.note}
            if item.name == "identity" and value == "<not sent>":
                entry["identity_matches_profile"] = None
                entry["identity_verification_skipped"] = True
            elif item.name == "identity" and not identity_matches(profile, value):
                entry["ok"] = False
                entry["identity_matches_profile"] = False
                entry["error"] = (
                    f"identity does not match selected profile {profile.key!r}; "
                    "no further queries were sent"
                )
                stopped_after_identity_mismatch = True
            elif item.name == "identity":
                entry["identity_matches_profile"] = True
        except Exception as exc:  # Preserve a diagnostic without attempting error-queue reads.
            entry["ok"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
            stopped_after_error = True
        entry["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 3)
        transcript.append(entry)
        if stopped_after_error or stopped_after_identity_mismatch:
            break

    finished = datetime.now(timezone.utc)
    return {
        "schema_version": 2,
        "started_utc": started.isoformat(),
        "finished_utc": finished.isoformat(),
        "profile": profile.key,
        "instrument_model": profile.model,
        "command_set": profile.command_set,
        "scope": scope,
        "transport": transport.backend_name,
        "visa_resource": getattr(transport, "resource_name", None),
        "safety": {
            "query_only": True,
            "exact_allowlist": True,
            "generic_write_api_exposed": False,
            "acquisition_trigger_queries_blocked": True,
            "event_and_error_queue_queries_blocked": True,
            "stopped_after_first_io_error": stopped_after_error,
            "stopped_after_identity_mismatch": stopped_after_identity_mismatch,
        },
        "host_environment": host_environment(),
        "instrument": results,
        "transcript": transcript,
    }
