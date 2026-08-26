"""Production GPIB assignments confirmed in NI MAX on 2026-08-19.

This module is intentionally laboratory-specific. Real-mode commands use
these exact resource/profile mappings so an old LabVIEW default or a mistyped
address cannot silently select a different instrument.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class LabInstrumentAssignment:
    key: str
    expected_model: str
    gpib_address: int
    allowed_profiles: tuple[str, ...]
    evidence: str
    command_set_note: str = ""

    @property
    def resource(self) -> str:
        return f"GPIB0::{self.gpib_address}::INSTR"

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["resource"] = self.resource
        value["status"] = "confirmed_in_ni_max"
        return value


LAB_INSTRUMENTS = (
    LabInstrumentAssignment(
        "2182a-gpib6",
        "2182A",
        6,
        ("2182a",),
        "NI MAX listed MODEL 2182A at GPIB0::6::INSTR on 2026-08-19.",
    ),
    LabInstrumentAssignment(
        "2182a-gpib7",
        "2182A",
        7,
        ("2182a",),
        "NI MAX listed MODEL 2182A at GPIB0::7::INSTR on 2026-08-19.",
    ),
    LabInstrumentAssignment(
        "6221-gpib9",
        "6221",
        9,
        ("6221",),
        "NI MAX listed MODEL 6221 at GPIB0::9::INSTR on 2026-08-19.",
    ),
    LabInstrumentAssignment(
        "6221-gpib10",
        "6221",
        10,
        ("6221",),
        "NI MAX listed MODEL 6221 at GPIB0::10::INSTR on 2026-08-19.",
    ),
    LabInstrumentAssignment(
        "2450-gpib25",
        "2450",
        25,
        ("2450-scpi", "2450-tsp"),
        "NI MAX listed MODEL 2450 at GPIB0::25::INSTR on 2026-08-19.",
        "Identity-only is safe with either profile. Confirm SCPI or TSP at the front panel before core queries.",
    ),
    LabInstrumentAssignment(
        "2450-gpib26",
        "2450",
        26,
        ("2450-scpi", "2450-tsp"),
        "NI MAX listed MODEL 2450 at GPIB0::26::INSTR on 2026-08-19.",
        "Identity-only is safe with either profile. Confirm SCPI or TSP at the front panel before core queries.",
    ),
)


def assignment_dicts() -> list[dict[str, object]]:
    return [assignment.as_dict() for assignment in LAB_INSTRUMENTS]


def assignment_for_resource(resource: str) -> LabInstrumentAssignment | None:
    normalized = resource.strip().upper()
    return next(
        (item for item in LAB_INSTRUMENTS if item.resource.upper() == normalized),
        None,
    )


def validate_confirmed_target(profile_key: str, resource: str, scope: str) -> None:
    assignment = assignment_for_resource(resource)
    if assignment is None:
        raise ValueError(
            f"resource is not in the NI MAX confirmed laboratory map: {resource!r}"
        )
    if profile_key not in assignment.allowed_profiles:
        allowed = ", ".join(assignment.allowed_profiles)
        raise ValueError(
            f"profile {profile_key!r} is not allowed for {assignment.resource}; "
            f"expected one of: {allowed}"
        )
    if assignment.expected_model == "2450" and scope != "identity":
        if profile_key not in {"2450-scpi", "2450-tsp"}:
            raise ValueError("2450 core/full reads require an explicit command-set profile")


def detect_model(identity: str) -> str | None:
    upper = identity.upper()
    if "2182A" in upper or "MODEL 2182" in upper:
        return "2182A"
    if "6221" in upper:
        return "6221"
    if "2450" in upper:
        return "2450"
    return None
