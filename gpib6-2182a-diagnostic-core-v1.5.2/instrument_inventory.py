"""Explicit, identity-only inventory refresh for the GPIB0 laboratory bus.

Importing this module performs no VISA operation.  ``refresh_inventory`` must
be called explicitly.  One refresh lists VISA resources exactly once, filters
only canonical GPIB0 primary-address INSTR resources, and sends exactly one
hard-coded ``*IDN?`` to each retained resource in sequential order.

The inventory layer never sends a model-specific query and never grants Live
capability.  A recognised model merely selects an immutable diagnostic profile
key for a later, separately authorised diagnostic run.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from diagnostic_core import DeviceIdentity, normalize_token, parse_idn


IDENTITY_QUERY = "*IDN?"
INVENTORY_TIMEOUT_MS = 2000
_GPIB0_PRIMARY_INSTR = re.compile(
    r"GPIB0::(?P<address>[1-9]|[12][0-9]|30)::INSTR",
    flags=re.IGNORECASE | re.ASCII,
)


@dataclass(frozen=True)
class KnownTspAsset:
    vendor: str
    model: str
    serial: str
    firmware: str


@dataclass(frozen=True)
class SimulatedInstrument:
    resource: str
    idn_raw: str


@dataclass(frozen=True)
class InventoryEntry:
    resource: str
    idn_raw: str | None
    identity: DeviceIdentity | None
    profile_key: str | None
    profile_resolution: str
    status: str
    elapsed_ms: float
    error: str | None
    live_supported: bool = False

    def as_dict(self) -> dict[str, object]:
        identity = None
        if self.identity is not None:
            identity = {
                "raw": self.identity.raw,
                "vendor": self.identity.vendor,
                "model": self.identity.model,
                "serial": self.identity.serial,
                "firmware": self.identity.firmware,
            }
        return {
            "resource": self.resource,
            "idn_raw": self.idn_raw,
            "identity": identity,
            "profile_key": self.profile_key,
            "profile_resolution": self.profile_resolution,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "live_supported": self.live_supported,
        }


@dataclass(frozen=True)
class InventoryCounts:
    raw_resource_count: int
    filtered_gpib_count: int
    identity_response_count: int
    parsed_identity_count: int
    recognized_profile_count: int
    unknown_model_count: int
    malformed_identity_count: int
    command_set_ambiguous_count: int
    io_error_count: int

    def as_dict(self) -> dict[str, int]:
        return {
            "raw_resource_count": self.raw_resource_count,
            "filtered_gpib_count": self.filtered_gpib_count,
            "identity_response_count": self.identity_response_count,
            "parsed_identity_count": self.parsed_identity_count,
            "recognized_profile_count": self.recognized_profile_count,
            "unknown_model_count": self.unknown_model_count,
            "malformed_identity_count": self.malformed_identity_count,
            "command_set_ambiguous_count": self.command_set_ambiguous_count,
            "io_error_count": self.io_error_count,
        }


@dataclass(frozen=True)
class InventorySnapshot:
    snapshot_id: str
    created_at_local: str
    source: str
    raw_resources: tuple[str, ...]
    filtered_resources: tuple[str, ...]
    entries: tuple[InventoryEntry, ...]
    counts: InventoryCounts
    refresh_error: str | None = None
    manager_close_error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "snapshot_id": self.snapshot_id,
            "created_at_local": self.created_at_local,
            "operation": "explicit query-only GPIB0 inventory refresh",
            "source": self.source,
            "safety": {
                "explicit_refresh_required": True,
                "list_resources_calls": 1 if self.refresh_error is None or self.raw_resources else None,
                "resource_filter": "GPIB0 primary addresses 1-30, INSTR only",
                "identity_query": IDENTITY_QUERY,
                "identity_queries_per_resource_max": 1,
                "timeout_ms": INVENTORY_TIMEOUT_MS,
                "sequential": True,
                "retry": False,
                "clear_reset_or_generic_query": False,
                "live_granted": False,
            },
            "raw_resources": list(self.raw_resources),
            "filtered_resources": list(self.filtered_resources),
            "entries": [entry.as_dict() for entry in self.entries],
            "counts": self.counts.as_dict(),
            "refresh_error": self.refresh_error,
            "manager_close_error": self.manager_close_error,
        }


TspPolicy = Callable[[str, DeviceIdentity], bool]


PROFILE_KEY_BY_EXACT_MODEL = MappingProxyType(
    {
        "2182A": "2182a",
        "6221": "6221",
        # A 2450 needs a separate TSP policy decision; *IDN? alone does not
        # reveal which command language is active.
        "2450": "2450",
    }
)

_KEITHLEY_VENDORS = frozenset(
    {
        "KEITHLEY INSTRUMENTS",
        "KEITHLEY INSTRUMENTS INC.",
    }
)


KNOWN_TSP_ASSETS = MappingProxyType(
    {
        "GPIB0::25::INSTR": KnownTspAsset(
            vendor="KEITHLEY INSTRUMENTS",
            model="2450",
            serial="04584128",
            firmware="1.7.12b",
        ),
    }
)


HANDOFF_SIMULATED_INSTRUMENTS = (
    SimulatedInstrument(
        "GPIB0::6::INSTR",
        "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,1340129,C02  /A02",
    ),
    SimulatedInstrument(
        "GPIB0::7::INSTR",
        "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,4510267,C08/B01",
    ),
    SimulatedInstrument(
        "GPIB0::9::INSTR",
        "KEITHLEY INSTRUMENTS INC.,MODEL 6221,4533811,D04  /700x",
    ),
    SimulatedInstrument(
        "GPIB0::10::INSTR",
        "KEITHLEY INSTRUMENTS INC.,MODEL 6221,4581062,D04  /700x",
    ),
    SimulatedInstrument(
        "GPIB0::25::INSTR",
        "KEITHLEY INSTRUMENTS,MODEL 2450,04584128,1.7.12b",
    ),
)


def local_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def filter_gpib0_resources(resources: Iterable[object]) -> tuple[str, ...]:
    """Return unique canonical GPIB0 primary INSTR resources in address order.

    The function never constructs a missing address.  Each returned value is
    the original string supplied by VISA, not a rewritten resource name.
    """

    retained: dict[str, tuple[int, str]] = {}
    for item in resources:
        rendered = str(item)
        match = _GPIB0_PRIMARY_INSTR.fullmatch(rendered)
        if match is None:
            continue
        normalized = rendered.upper()
        retained.setdefault(normalized, (int(match.group("address")), rendered))
    return tuple(
        original
        for _address, original in sorted(
            retained.values(),
            key=lambda item: (item[0], item[1].upper()),
        )
    )


def _identity_matches_asset(identity: DeviceIdentity, asset: KnownTspAsset) -> bool:
    return all(
        normalize_token(observed) == normalize_token(expected)
        for observed, expected in (
            (identity.vendor, asset.vendor),
            (identity.model, asset.model),
            (identity.serial, asset.serial),
            (identity.firmware, asset.firmware),
        )
    )


def _asset_for_resource(
    resource: str,
    known_tsp_assets: Mapping[str, KnownTspAsset],
) -> KnownTspAsset | None:
    normalized = resource.upper()
    for candidate_resource, asset in known_tsp_assets.items():
        if str(candidate_resource).upper() == normalized:
            return asset
    return None


def _resolve_profile(
    resource: str,
    identity: DeviceIdentity,
    *,
    tsp_policy: TspPolicy | None,
    known_tsp_assets: Mapping[str, KnownTspAsset],
) -> tuple[str | None, str, str, str | None]:
    vendor = normalize_token(identity.vendor)
    model = normalize_token(identity.model)
    if vendor not in _KEITHLEY_VENDORS:
        return None, "unsupported_vendor", "unknown_model", None
    generic_key = PROFILE_KEY_BY_EXACT_MODEL.get(model)
    if generic_key is None:
        return None, "unknown_exact_model", "unknown_model", None
    if model == "2182A":
        return "2182a", "exact_model", "recognized", None
    if model == "6221":
        return "6221", "exact_model", "recognized", None

    try:
        if tsp_policy is not None:
            tsp_approved = bool(tsp_policy(resource, identity))
            resolution = "tsp_policy_callback"
        else:
            asset = _asset_for_resource(resource, known_tsp_assets)
            tsp_approved = asset is not None and _identity_matches_asset(identity, asset)
            resolution = "known_tsp_asset_exact_identity"
    except Exception as exc:
        error = f"TSP policy failed: {type(exc).__name__}: {exc}"
        return None, "tsp_policy_error", "command_set_ambiguous", error
    if tsp_approved:
        return "2450", resolution, "recognized", None
    return None, "2450_command_set_unconfirmed", "command_set_ambiguous", None


def _classify_identity(
    resource: str,
    idn_raw: str,
    *,
    elapsed_ms: float,
    tsp_policy: TspPolicy | None,
    known_tsp_assets: Mapping[str, KnownTspAsset],
) -> InventoryEntry:
    try:
        identity = parse_idn(idn_raw)
    except Exception as exc:
        return InventoryEntry(
            resource=resource,
            idn_raw=idn_raw,
            identity=None,
            profile_key=None,
            profile_resolution="identity_parse_failed",
            status="malformed_identity",
            elapsed_ms=elapsed_ms,
            error=f"{type(exc).__name__}: {exc}",
        )
    profile_key, resolution, status, policy_error = _resolve_profile(
        resource,
        identity,
        tsp_policy=tsp_policy,
        known_tsp_assets=known_tsp_assets,
    )
    return InventoryEntry(
        resource=resource,
        idn_raw=idn_raw,
        identity=identity,
        profile_key=profile_key,
        profile_resolution=resolution,
        status=status,
        elapsed_ms=elapsed_ms,
        error=policy_error,
    )


def _combine_errors(first: str | None, second: str) -> str:
    return second if first is None else f"{first}; {second}"


def _counts(
    raw_resources: tuple[str, ...],
    filtered_resources: tuple[str, ...],
    entries: tuple[InventoryEntry, ...],
) -> InventoryCounts:
    return InventoryCounts(
        raw_resource_count=len(raw_resources),
        filtered_gpib_count=len(filtered_resources),
        identity_response_count=sum(entry.idn_raw is not None for entry in entries),
        parsed_identity_count=sum(entry.identity is not None for entry in entries),
        recognized_profile_count=sum(entry.profile_key is not None for entry in entries),
        unknown_model_count=sum(entry.status == "unknown_model" for entry in entries),
        malformed_identity_count=sum(
            entry.status == "malformed_identity" for entry in entries
        ),
        command_set_ambiguous_count=sum(
            entry.status == "command_set_ambiguous" for entry in entries
        ),
        io_error_count=sum(
            entry.status in {"io_error", "resource_close_error"}
            for entry in entries
        ),
    )


def _snapshot(
    *,
    source: str,
    raw_resources: tuple[str, ...],
    filtered_resources: tuple[str, ...],
    entries: tuple[InventoryEntry, ...],
    refresh_error: str | None = None,
    manager_close_error: str | None = None,
) -> InventorySnapshot:
    return InventorySnapshot(
        snapshot_id=str(uuid.uuid4()),
        created_at_local=local_iso(),
        source=source,
        raw_resources=raw_resources,
        filtered_resources=filtered_resources,
        entries=entries,
        counts=_counts(raw_resources, filtered_resources, entries),
        refresh_error=refresh_error,
        manager_close_error=manager_close_error,
    )


def _default_resource_manager_factory():
    try:
        import pyvisa
    except ImportError as exc:
        raise RuntimeError(
            "PyVISA is missing from C:\\LabAutomation\\.venv"
        ) from exc
    return pyvisa.ResourceManager()


def refresh_inventory(
    *,
    resource_manager_factory=None,
    tsp_policy: TspPolicy | None = None,
    known_tsp_assets: Mapping[str, KnownTspAsset] = KNOWN_TSP_ASSETS,
) -> InventorySnapshot:
    """Perform one explicit, sequential, identity-only inventory refresh.

    Per-resource failures are recorded and scanning continues.  No retry or
    recovery message is sent.  The caller may run this blocking function in one
    background worker, but this function itself creates no thread and never has
    more than one resource open at a time.
    """

    factory = resource_manager_factory or _default_resource_manager_factory
    manager = None
    raw_resources: tuple[str, ...] = ()
    filtered_resources: tuple[str, ...] = ()
    entries: list[InventoryEntry] = []
    refresh_error: str | None = None
    manager_close_error: str | None = None

    try:
        manager = factory()
        # Deliberately exactly one call.  Do not re-enumerate after failures.
        raw_resources = tuple(str(item) for item in manager.list_resources())
        filtered_resources = filter_gpib0_resources(raw_resources)
        for resource_name in filtered_resources:
            resource = None
            idn_raw: str | None = None
            entry: InventoryEntry | None = None
            started = time.perf_counter()
            try:
                resource = manager.open_resource(resource_name)
                resource.timeout = INVENTORY_TIMEOUT_MS
                # This is the only instrument message in the inventory path.
                idn_raw = str(resource.query(IDENTITY_QUERY)).rstrip("\r\n")
                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
                entry = _classify_identity(
                    resource_name,
                    idn_raw,
                    elapsed_ms=elapsed_ms,
                    tsp_policy=tsp_policy,
                    known_tsp_assets=known_tsp_assets,
                )
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
                entry = InventoryEntry(
                    resource=resource_name,
                    idn_raw=idn_raw,
                    identity=None,
                    profile_key=None,
                    profile_resolution="identity_io_failed",
                    status="io_error",
                    elapsed_ms=elapsed_ms,
                    error=f"{type(exc).__name__}: {exc}",
                )
            finally:
                if resource is not None:
                    try:
                        resource.close()
                    except Exception as exc:
                        close_error = f"resource close failed: {type(exc).__name__}: {exc}"
                        if entry is None:
                            elapsed_ms = round(
                                (time.perf_counter() - started) * 1000.0,
                                3,
                            )
                            entry = InventoryEntry(
                                resource=resource_name,
                                idn_raw=idn_raw,
                                identity=None,
                                profile_key=None,
                                profile_resolution="resource_close_failed",
                                status="resource_close_error",
                                elapsed_ms=elapsed_ms,
                                error=close_error,
                            )
                        else:
                            entry = InventoryEntry(
                                resource=entry.resource,
                                idn_raw=entry.idn_raw,
                                identity=entry.identity,
                                profile_key=entry.profile_key,
                                profile_resolution=entry.profile_resolution,
                                status="resource_close_error",
                                elapsed_ms=entry.elapsed_ms,
                                error=_combine_errors(entry.error, close_error),
                                live_supported=False,
                            )
            if entry is None:  # Defensive; every path above must create one.
                raise RuntimeError(f"inventory produced no entry for {resource_name!r}")
            entries.append(entry)
    except Exception as exc:
        refresh_error = f"{type(exc).__name__}: {exc}"
    finally:
        if manager is not None:
            try:
                manager.close()
            except Exception as exc:
                manager_close_error = (
                    f"resource manager close failed: {type(exc).__name__}: {exc}"
                )

    return _snapshot(
        source="real",
        raw_resources=raw_resources,
        filtered_resources=filtered_resources,
        entries=tuple(entries),
        refresh_error=refresh_error,
        manager_close_error=manager_close_error,
    )


def build_simulated_inventory(
    fixtures: Iterable[SimulatedInstrument] = HANDOFF_SIMULATED_INSTRUMENTS,
    *,
    tsp_policy: TspPolicy | None = None,
    known_tsp_assets: Mapping[str, KnownTspAsset] = KNOWN_TSP_ASSETS,
) -> InventorySnapshot:
    """Build deterministic offline inventory data without importing PyVISA."""

    fixture_values = tuple(fixtures)
    raw_resources = tuple(item.resource for item in fixture_values)
    filtered_resources = filter_gpib0_resources(raw_resources)
    by_resource = {item.resource.upper(): item for item in fixture_values}
    entries = tuple(
        _classify_identity(
            resource,
            by_resource[resource.upper()].idn_raw,
            elapsed_ms=0.0,
            tsp_policy=tsp_policy,
            known_tsp_assets=known_tsp_assets,
        )
        for resource in filtered_resources
    )
    return _snapshot(
        source="simulate",
        raw_resources=raw_resources,
        filtered_resources=filtered_resources,
        entries=entries,
    )


__all__ = (
    "HANDOFF_SIMULATED_INSTRUMENTS",
    "IDENTITY_QUERY",
    "INVENTORY_TIMEOUT_MS",
    "InventoryCounts",
    "InventoryEntry",
    "InventorySnapshot",
    "KNOWN_TSP_ASSETS",
    "KnownTspAsset",
    "PROFILE_KEY_BY_EXACT_MODEL",
    "SimulatedInstrument",
    "TspPolicy",
    "build_simulated_inventory",
    "filter_gpib0_resources",
    "refresh_inventory",
)
