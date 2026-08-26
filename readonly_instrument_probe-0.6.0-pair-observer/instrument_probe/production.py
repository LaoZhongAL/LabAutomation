from __future__ import annotations

import platform
import struct
import sys


PRODUCTION_POLICY = (
    "Real VISA/GPIB communication is permitted only on the laboratory Windows "
    "x64 control computer. macOS is code-generation, review, and simulation only. "
    "Python bitness is recorded but must be validated against the installed VISA "
    "library by successfully enumerating VISA resources."
)


def evaluate_production_host(
    *,
    system: str,
    machine: str,
    python_bits: int,
    python_version: tuple[int, int],
) -> dict[str, object]:
    normalized_machine = machine.casefold()
    checks = {
        "windows": system.casefold() == "windows",
        "x86_64_machine": normalized_machine in {"amd64", "x86_64"},
        "python_3_9_or_newer": python_version >= (3, 9),
    }
    labels = {
        "windows": "operating system is not Windows",
        "x86_64_machine": "machine architecture is not x86-64/AMD64",
        "python_3_9_or_newer": "Python version is older than the project's 3.9 minimum",
    }
    blockers = [labels[name] for name, passed in checks.items() if not passed]
    return {
        "policy": PRODUCTION_POLICY,
        "system": system,
        "machine": machine,
        "python_bits": python_bits,
        "python_version": f"{python_version[0]}.{python_version[1]}",
        "checks": checks,
        "host_gate_passed": not blockers,
        "readiness_scope": (
            "Host and project compatibility only. NI-VISA/Python bitness compatibility "
            "must be verified by successful VISA resource enumeration before any query."
        ),
        "python_bitness_assessment": {
            "detected_bits": python_bits,
            "is_hard_gate": False,
            "required_condition": (
                "The loaded NI-VISA library must have the same bitness as Python; "
                "either 32-bit or 64-bit can be valid."
            ),
        },
        "blockers": blockers,
    }


def production_host_readiness() -> dict[str, object]:
    return evaluate_production_host(
        system=platform.system(),
        machine=platform.machine(),
        python_bits=struct.calcsize("P") * 8,
        python_version=(sys.version_info.major, sys.version_info.minor),
    )


def require_production_host() -> dict[str, object]:
    readiness = production_host_readiness()
    if not readiness["host_gate_passed"]:
        blockers = "; ".join(readiness["blockers"])
        raise RuntimeError(
            "real VISA/GPIB access is blocked on this host: "
            f"{blockers}. {PRODUCTION_POLICY}"
        )
    return readiness
