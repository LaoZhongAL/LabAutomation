"""Thread-safe manifest and event journal for one GPIB6 diagnostic run."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from diagnostic_core import (
    ALLOWED_STATE_TRANSITIONS,
    DIAGNOSTIC_SCHEMA_VERSION,
    PROFILE_ID,
    DiagnosticState,
    DiagnosticStateMachine,
    InvalidStateTransition,
    target_as_dict,
)


APP_NAME = "gpib6-2182a-live-monitor"
APP_VERSION = "1.1.0-diagnostics-v1.1"
EVENT_SCHEMA_VERSION = 1
INTERVENTION_SCHEMA_VERSION = 1
INTERVENTION_TYPES = (
    "cable_disturbance",
    "connector_disturbance",
    "interface_mechanical_stress",
    "other",
)


class RecorderError(RuntimeError):
    pass


def local_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def query_policy_hash(allowed_queries: Iterable[str]) -> str:
    rendered = json.dumps(sorted(set(allowed_queries)), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(rendered.encode("ascii")).hexdigest()


class RunJournal:
    """Own the lifecycle state, run manifest, and sparse JSONL event stream."""

    def __init__(
        self,
        run_directory: Path,
        *,
        mode: str,
        allowed_queries: Iterable[str],
        fault_scenario: str = "nominal",
        simulation_seed: int = 2182,
        fail_event_write_after: int | None = None,
        fail_intervention_write_after: int | None = None,
    ) -> None:
        self.run_directory = Path(run_directory)
        if not self.run_directory.is_dir():
            raise RecorderError(f"run directory does not exist: {self.run_directory}")
        if mode not in {"simulate", "real"}:
            raise RecorderError(f"unsupported run mode: {mode!r}")
        if mode == "real" and fault_scenario != "nominal":
            raise RecorderError("fault injection is forbidden in real mode evidence")
        query_items = list(allowed_queries)
        if not query_items:
            raise RecorderError("query allowlist must not be empty")
        unsafe_queries = [
            command
            for command in query_items
            if not isinstance(command, str)
            or command != command.strip()
            or not command.endswith("?")
            or ";" in command
            or "\n" in command
            or "\r" in command
        ]
        if unsafe_queries:
            raise RecorderError(f"non-query or compound allowlist entries: {unsafe_queries!r}")
        queries = sorted(set(query_items))
        self.manifest_path = self.run_directory / "run-manifest.json"
        self.events_path = self.run_directory / "events.jsonl"
        self.interventions_path = self.run_directory / "interventions.jsonl"
        if (
            self.manifest_path.exists()
            or self.events_path.exists()
            or self.interventions_path.exists()
        ):
            raise RecorderError("manifest, event journal, or intervention journal already exists")

        self._lock = threading.RLock()
        self._started_monotonic = time.monotonic()
        self._last_event_elapsed = 0.0
        self._sequence = 0
        self._intervention_sequence = 0
        self._active_interventions: dict[str, dict[str, object]] = {}
        self._closed = False
        self._fail_event_write_after = fail_event_write_after
        self._fail_intervention_write_after = fail_intervention_write_after
        self.state_machine = DiagnosticStateMachine()

        self.manifest: dict[str, object] = {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "run_id": str(uuid.uuid4()),
            "app": {"name": APP_NAME, "version": APP_VERSION},
            "created_at_local": local_iso(),
            "mode": mode,
            "profile_id": PROFILE_ID,
            "target": target_as_dict(),
            "safety": {
                "query_only": True,
                "exact_allowlist": True,
                "generic_write_api_exposed": False,
                "configuration_controls_exposed": False,
                "allowed_queries": queries,
                "policy_sha256": query_policy_hash(queries),
            },
            "fault_injection": {
                "enabled": mode == "simulate" and fault_scenario != "nominal",
                "scenario": fault_scenario if mode == "simulate" else "nominal",
                "seed": simulation_seed if mode == "simulate" else None,
            },
            "state": self.state_machine.state.value,
            "observed_identity": None,
            "readiness": None,
            "artifacts": {
                "manifest": self.manifest_path.name,
                "events": self.events_path.name,
                "interventions": self.interventions_path.name,
                "configuration_snapshot": None,
                "streams": [],
            },
            "statistics": {
                "sample_count": 0,
                "intervention_count": 0,
                "error_count": 0,
            },
            "final": {
                "closed": False,
                "finished_at_local": None,
                "duration_seconds": None,
                "state": self.state_machine.state.value,
                "termination_reason": None,
            },
        }

        try:
            self.events_path.touch(exist_ok=False)
            self.interventions_path.touch(exist_ok=False)
            self._atomic_write_manifest()
            self.record_event(
                "run_created",
                reason_code="RUN_CREATED",
                payload={"mode": mode, "fault_scenario": fault_scenario},
            )
        except Exception as exc:
            raise RecorderError(f"cannot initialize run evidence: {type(exc).__name__}: {exc}") from exc

    @property
    def run_id(self) -> str:
        return str(self.manifest["run_id"])

    @property
    def state(self) -> DiagnosticState:
        return self.state_machine.state

    def _elapsed(self, supplied: float | None = None) -> float:
        value = time.monotonic() - self._started_monotonic if supplied is None else supplied
        value = max(self._last_event_elapsed, max(0.0, float(value)))
        self._last_event_elapsed = value
        return value

    def _atomic_write_manifest(self) -> None:
        temporary = self.manifest_path.with_suffix(".json.tmp")
        rendered = json.dumps(self.manifest, indent=2, ensure_ascii=False) + "\n"
        try:
            temporary.write_text(rendered, encoding="utf-8")
            os.replace(temporary, self.manifest_path)
        except Exception as exc:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
            raise RecorderError(f"manifest write failed: {type(exc).__name__}: {exc}") from exc

    def record_event(
        self,
        event_type: str,
        *,
        severity: str = "INFO",
        reason_code: str,
        payload: Mapping[str, object] | None = None,
        elapsed_seconds: float | None = None,
        stream_id: str | None = None,
        _state_override: DiagnosticState | None = None,
    ) -> dict[str, object]:
        with self._lock:
            if self._closed:
                raise RecorderError("event journal is already closed")
            if self._fail_event_write_after is not None and self._sequence >= self._fail_event_write_after:
                raise RecorderError("simulated event journal write failure")
            self._sequence += 1
            event = {
                "schema_version": EVENT_SCHEMA_VERSION,
                "run_id": self.run_id,
                "seq": self._sequence,
                "elapsed_seconds": round(self._elapsed(elapsed_seconds), 6),
                "event_type": event_type,
                "severity": severity,
                "state": (_state_override or self.state).value,
                "reason_code": reason_code,
                "stream_id": stream_id,
                "payload": dict(payload or {}),
            }
            try:
                with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                    handle.flush()
            except Exception as exc:
                raise RecorderError(f"event journal write failed: {type(exc).__name__}: {exc}") from exc
            return event

    def transition(
        self,
        target: DiagnosticState,
        *,
        reason_code: str,
        payload: Mapping[str, object] | None = None,
        severity: str = "INFO",
    ) -> None:
        with self._lock:
            before = self.state
            after = target
            if after != before and after not in ALLOWED_STATE_TRANSITIONS[before]:
                raise InvalidStateTransition(
                    f"invalid diagnostic transition: {before.value} -> {after.value}"
                )
            previous_manifest_state = self.manifest.get("state")
            final = self.manifest["final"]
            previous_final_state = final.get("state") if isinstance(final, dict) else None
            self.manifest["state"] = after.value
            if isinstance(final, dict):
                final["state"] = after.value
            try:
                self._atomic_write_manifest()
                self.record_event(
                    "state_transition",
                    severity=severity,
                    reason_code=reason_code,
                    payload={
                        "from_state": before.value,
                        "to_state": after.value,
                        **dict(payload or {}),
                    },
                    _state_override=after,
                )
            except Exception as exc:
                self.manifest["state"] = previous_manifest_state
                if isinstance(final, dict):
                    final["state"] = previous_final_state
                try:
                    self._atomic_write_manifest()
                except Exception as rollback_exc:
                    raise RecorderError(
                        "state transition failed and manifest rollback also failed: "
                        f"transition={type(exc).__name__}: {exc}; "
                        f"rollback={type(rollback_exc).__name__}: {rollback_exc}"
                    ) from rollback_exc
                raise
            self.state_machine.transition(after)

    def set_configuration_snapshot(self, filename: str) -> None:
        with self._lock:
            artifacts = self.manifest["artifacts"]
            if isinstance(artifacts, dict):
                artifacts["configuration_snapshot"] = filename
            self.record_event(
                "configuration_snapshot_persisted",
                reason_code="CONFIGURATION_SNAPSHOT_PERSISTED",
                payload={"path": filename},
            )
            self._atomic_write_manifest()

    def set_diagnostics(
        self,
        *,
        observed_identity: Mapping[str, object] | None,
        readiness: Mapping[str, object],
    ) -> None:
        with self._lock:
            self.manifest["observed_identity"] = dict(observed_identity or {}) or None
            self.manifest["readiness"] = dict(readiness)
            self.record_event(
                "readiness_evaluated",
                severity="INFO" if readiness.get("can_start_live") else "ERROR",
                reason_code=(
                    "READINESS_OBSERVE_READY"
                    if readiness.get("can_start_live")
                    else "READINESS_BLOCKED"
                ),
                payload={
                    "overall": readiness.get("overall"),
                    "can_start_live": readiness.get("can_start_live"),
                    "blockers": readiness.get("blockers", []),
                    "warnings": readiness.get("warnings", []),
                },
            )
            self._atomic_write_manifest()

    def register_stream(self, csv_path: Path) -> str:
        with self._lock:
            stream_id = str(uuid.uuid4())
            artifacts = self.manifest["artifacts"]
            if not isinstance(artifacts, dict):
                raise RecorderError("manifest artifacts structure is invalid")
            streams = artifacts.setdefault("streams", [])
            if not isinstance(streams, list):
                raise RecorderError("manifest stream structure is invalid")
            streams.append(
                {
                    "stream_id": stream_id,
                    "csv": Path(csv_path).name,
                    "status": "requested",
                    "sample_count": 0,
                    "intervention_count": 0,
                    "last_elapsed_seconds": None,
                    "outcome": None,
                    "error": None,
                    "fault_injection": {
                        "scenario": None,
                        "consumed_rule_ids": [],
                        "query_history": [],
                    },
                }
            )
            self.record_event(
                "stream_start_requested",
                reason_code="STREAM_START_REQUESTED",
                payload={"csv": Path(csv_path).name},
                stream_id=stream_id,
            )
            self._atomic_write_manifest()
            return stream_id

    def _stream(self, stream_id: str) -> dict[str, object]:
        artifacts = self.manifest.get("artifacts", {})
        streams = artifacts.get("streams", []) if isinstance(artifacts, dict) else []
        for stream in streams if isinstance(streams, list) else []:
            if isinstance(stream, dict) and stream.get("stream_id") == stream_id:
                return stream
        raise RecorderError(f"unknown stream id: {stream_id}")

    def stream_started(self, stream_id: str) -> None:
        with self._lock:
            self._stream(stream_id)["status"] = "live"
            self.record_event(
                "stream_started",
                reason_code="STREAM_STARTED",
                stream_id=stream_id,
            )
            self._atomic_write_manifest()

    def set_stream_fault_evidence(
        self,
        stream_id: str,
        *,
        scenario: str,
        consumed_rule_ids: Iterable[str],
        query_history: Iterable[Mapping[str, object]],
    ) -> None:
        with self._lock:
            evidence = {
                "scenario": scenario,
                "consumed_rule_ids": [str(rule_id) for rule_id in consumed_rule_ids],
                "query_history": [dict(item) for item in query_history],
            }
            self._stream(stream_id)["fault_injection"] = evidence
            self.record_event(
                "stream_fault_evidence_persisted",
                reason_code="STREAM_FAULT_EVIDENCE_PERSISTED",
                payload={
                    "scenario": scenario,
                    "consumed_rule_ids": evidence["consumed_rule_ids"],
                    "query_count": len(evidence["query_history"]),
                },
                stream_id=stream_id,
            )
            self._atomic_write_manifest()

    def record_sample(self, stream_id: str, elapsed_seconds: float) -> None:
        with self._lock:
            stream = self._stream(stream_id)
            stream["sample_count"] = int(stream.get("sample_count", 0)) + 1
            stream["last_elapsed_seconds"] = round(float(elapsed_seconds), 6)
            statistics = self.manifest["statistics"]
            if isinstance(statistics, dict):
                statistics["sample_count"] = int(statistics.get("sample_count", 0)) + 1

    @staticmethod
    def _validated_intervention_elapsed(elapsed_seconds: float) -> float:
        value = float(elapsed_seconds)
        if not math.isfinite(value) or value < 0:
            raise RecorderError(f"invalid intervention elapsed time: {elapsed_seconds!r}")
        return value

    @staticmethod
    def _validated_intervention_text(intervention_type: str, location: str) -> tuple[str, str]:
        normalized_type = str(intervention_type).strip()
        normalized_location = str(location).strip()
        if normalized_type not in INTERVENTION_TYPES:
            raise RecorderError(f"unsupported intervention type: {intervention_type!r}")
        if not normalized_location:
            raise RecorderError("intervention location must not be empty")
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized_location):
            raise RecorderError("intervention location must be one printable line")
        return normalized_type, normalized_location

    def _append_intervention(self, record: Mapping[str, object]) -> None:
        if self._closed:
            raise RecorderError("intervention journal is already closed")
        if (
            self._fail_intervention_write_after is not None
            and self._intervention_sequence >= self._fail_intervention_write_after
        ):
            raise RecorderError("simulated intervention journal write failure")
        try:
            with self.interventions_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
        except Exception as exc:
            raise RecorderError(
                f"intervention journal write failed: {type(exc).__name__}: {exc}"
            ) from exc

    def start_intervention(
        self,
        stream_id: str,
        *,
        elapsed_seconds: float,
        intervention_type: str,
        location: str,
    ) -> dict[str, object]:
        with self._lock:
            stream = self._stream(stream_id)
            if any(
                active.get("stream_id") == stream_id
                for active in self._active_interventions.values()
            ):
                raise RecorderError("this stream already has an active intervention")
            if stream.get("status") != "live":
                raise RecorderError("interventions require a live stream with a committed sample")
            if self.state not in {DiagnosticState.LIVE, DiagnosticState.DEGRADED}:
                raise RecorderError(
                    f"interventions require LIVE or DEGRADED state, got {self.state.value}"
                )
            normalized_type, normalized_location = self._validated_intervention_text(
                intervention_type,
                location,
            )
            elapsed = self._validated_intervention_elapsed(elapsed_seconds)
            intervention_id = str(uuid.uuid4())
            next_sequence = self._intervention_sequence + 1
            record: dict[str, object] = {
                "schema_version": INTERVENTION_SCHEMA_VERSION,
                "run_id": self.run_id,
                "seq": next_sequence,
                "stream_id": stream_id,
                "intervention_id": intervention_id,
                "phase": "start",
                "elapsed_seconds": round(elapsed, 6),
                "intervention_type": normalized_type,
                "location": normalized_location,
            }
            self._append_intervention(record)
            self._intervention_sequence = next_sequence
            self._active_interventions[intervention_id] = dict(record)
            return dict(record)

    def end_intervention(
        self,
        intervention_id: str,
        *,
        elapsed_seconds: float,
    ) -> dict[str, object]:
        with self._lock:
            active = self._active_interventions.get(intervention_id)
            if active is None:
                raise RecorderError(f"unknown or inactive intervention: {intervention_id}")
            elapsed = self._validated_intervention_elapsed(elapsed_seconds)
            start_elapsed = float(active["elapsed_seconds"])
            if elapsed < start_elapsed:
                raise RecorderError(
                    f"intervention end {elapsed!r} precedes start {start_elapsed!r}"
                )
            next_sequence = self._intervention_sequence + 1
            record: dict[str, object] = {
                "schema_version": INTERVENTION_SCHEMA_VERSION,
                "run_id": self.run_id,
                "seq": next_sequence,
                "stream_id": active["stream_id"],
                "intervention_id": intervention_id,
                "phase": "end",
                "elapsed_seconds": round(elapsed, 6),
                "intervention_type": active["intervention_type"],
                "location": active["location"],
            }
            self._append_intervention(record)
            self._intervention_sequence = next_sequence
            del self._active_interventions[intervention_id]
            stream = self._stream(str(active["stream_id"]))
            stream["intervention_count"] = int(stream.get("intervention_count", 0)) + 1
            statistics = self.manifest["statistics"]
            if isinstance(statistics, dict):
                statistics["intervention_count"] = int(
                    statistics.get("intervention_count", 0)
                ) + 1
            return dict(record)

    def stop_interventions_for_stream(
        self,
        stream_id: str,
        *,
        elapsed_seconds: float,
    ) -> dict[str, object] | None:
        with self._lock:
            stream = self._stream(stream_id)
            if stream.get("status") == "live":
                stream["status"] = "stopping"
            matching_ids = [
                intervention_id
                for intervention_id, active in self._active_interventions.items()
                if active.get("stream_id") == stream_id
            ]
            if not matching_ids:
                return None
            if len(matching_ids) != 1:
                raise RecorderError(
                    f"stream has multiple active interventions: {stream_id}"
                )
            return self.end_intervention(
                matching_ids[0],
                elapsed_seconds=elapsed_seconds,
            )

    def record_error(
        self,
        *,
        reason_code: str,
        message: str,
        stream_id: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        with self._lock:
            statistics = self.manifest["statistics"]
            if isinstance(statistics, dict):
                statistics["error_count"] = int(statistics.get("error_count", 0)) + 1
            merged = {"message": message, **dict(payload or {})}
            self.record_event(
                "fault",
                severity="ERROR",
                reason_code=reason_code,
                payload=merged,
                stream_id=stream_id,
            )
            self._atomic_write_manifest()

    def finish_stream(
        self,
        stream_id: str,
        *,
        outcome: str,
        error: str | None = None,
    ) -> None:
        with self._lock:
            has_active_intervention = any(
                active.get("stream_id") == stream_id
                for active in self._active_interventions.values()
            )
            if has_active_intervention and outcome != "fault":
                raise RecorderError(
                    f"cannot finish stream with an active intervention: {stream_id}"
                )
            stream = self._stream(stream_id)
            stream["status"] = "closed"
            stream["outcome"] = outcome
            stream["error"] = error
            self.record_event(
                "stream_stopped",
                severity="INFO" if error is None else "ERROR",
                reason_code="STREAM_STOPPED" if error is None else "STREAM_STOPPED_WITH_ERROR",
                payload={
                    "outcome": outcome,
                    "error": error,
                    "sample_count": stream.get("sample_count", 0),
                    "intervention_count": stream.get("intervention_count", 0),
                },
                stream_id=stream_id,
            )
            self._atomic_write_manifest()

    def finalize(self, reason: str) -> None:
        with self._lock:
            if self._closed:
                return
            self.record_event(
                "run_closed",
                reason_code="RUN_CLOSED",
                payload={"termination_reason": reason},
            )
            final = self.manifest["final"]
            if isinstance(final, dict):
                final.update(
                    {
                        "closed": True,
                        "finished_at_local": local_iso(),
                        "duration_seconds": round(time.monotonic() - self._started_monotonic, 6),
                        "state": self.state.value,
                        "termination_reason": reason,
                    }
                )
            self._atomic_write_manifest()
            self._closed = True
