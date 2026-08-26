"""Offline consistency checks for one completed diagnostic run directory."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from pathlib import Path

from diagnostic_core import parse_idn
from stream_quality import analyze_stream_csv


def _artifact_path(run_directory: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"artifact filename is missing: {value!r}")
    relative = Path(value)
    if relative.is_absolute() or relative.name != value:
        raise ValueError(f"artifact must be a filename inside the run directory: {value!r}")
    return run_directory / relative


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[object]:
    records: list[object] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise ValueError(f"{path.name} line {line_number} is blank")
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{path.name} line {line_number} is invalid JSON: {exc}"
            ) from exc
    return records


def _finite_nonnegative(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0


def verify_run_directory(run_directory: Path) -> dict[str, object]:
    """Return a fail-closed report; never alter the inspected evidence."""

    root = Path(run_directory)
    checks: list[dict[str, object]] = []
    errors: list[str] = []

    def record(check_id: str, passed: bool, detail: str) -> None:
        checks.append({"check_id": check_id, "passed": bool(passed), "detail": detail})
        if not passed:
            errors.append(f"{check_id}: {detail}")

    manifest_path = root / "run-manifest.json"
    try:
        manifest = _read_json(manifest_path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        record(
            "manifest.readable",
            False,
            f"{type(exc).__name__}: {exc}",
        )
        return {
            "schema_version": 1,
            "run_id": None,
            "passed": False,
            "checks": checks,
            "errors": errors,
        }
    if not isinstance(manifest, dict):
        record("manifest.object", False, "run-manifest.json is not a JSON object")
        return {
            "schema_version": 1,
            "run_id": None,
            "passed": False,
            "checks": checks,
            "errors": errors,
        }
    record("manifest.readable", True, "run-manifest.json is a JSON object")
    run_id = manifest.get("run_id")
    final = manifest.get("final")
    record(
        "manifest.final_closed",
        isinstance(final, dict) and final.get("closed") is True,
        "final.closed must be true",
    )
    safety = manifest.get("safety")
    record(
        "manifest.query_only",
        bool(
            isinstance(safety, dict)
            and safety.get("query_only") is True
            and safety.get("exact_allowlist") is True
            and safety.get("generic_write_api_exposed") is False
            and safety.get("configuration_controls_exposed") is False
        ),
        "query-only and exact-allowlist safety fields must remain asserted",
    )
    allowed_queries = (
        safety.get("allowed_queries") if isinstance(safety, dict) else None
    )
    valid_allowed_queries = bool(
        isinstance(allowed_queries, list)
        and allowed_queries
        and all(isinstance(command, str) for command in allowed_queries)
        and len(allowed_queries) == len(set(allowed_queries))
    )
    record(
        "manifest.allowed_queries",
        valid_allowed_queries,
        "allowed_queries must be a non-empty list of unique strings",
    )
    expected_policy_hash = None
    if valid_allowed_queries:
        rendered_policy = json.dumps(
            sorted(set(allowed_queries)),
            separators=(",", ":"),
            ensure_ascii=True,
        )
        expected_policy_hash = hashlib.sha256(
            rendered_policy.encode("ascii")
        ).hexdigest()
    record(
        "manifest.policy_sha256",
        bool(
            isinstance(safety, dict)
            and expected_policy_hash is not None
            and safety.get("policy_sha256") == expected_policy_hash
        ),
        "policy_sha256 must match the canonical exact allowlist",
    )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        record("manifest.artifacts", False, "artifacts is not an object")
        return {
            "schema_version": 1,
            "run_id": run_id,
            "passed": False,
            "checks": checks,
            "errors": errors,
        }
    record("manifest.artifacts", True, "artifacts is an object")

    configuration_name = artifacts.get("configuration_snapshot")
    if configuration_name is not None:
        try:
            configuration = _read_json(_artifact_path(root, configuration_name))
            record(
                "configuration_snapshot.readable",
                isinstance(configuration, dict),
                "referenced configuration snapshot must be a JSON object",
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            record(
                "configuration_snapshot.readable",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        else:
            if isinstance(configuration, dict):
                target = manifest.get("target")
                expected_identity = configuration.get("expected_identity")
                target_identity = (
                    {
                        field: target.get(field)
                        for field in ("vendor", "model", "serial", "firmware")
                    }
                    if isinstance(target, dict)
                    else None
                )
                manifest_target_key = (
                    target.get("key", target.get("role"))
                    if isinstance(target, dict)
                    else None
                )
                record(
                    "configuration_snapshot.target",
                    bool(
                        isinstance(target, dict)
                        and configuration.get("resource") == target.get("resource")
                        and configuration.get("target_key") == manifest_target_key
                        and expected_identity == target_identity
                    ),
                    "configuration resource, target_key, and expected identity must match manifest target",
                )
                record(
                    "configuration_snapshot.profile",
                    configuration.get("profile_id") == manifest.get("profile_id"),
                    "configuration profile_id must match manifest profile_id",
                )
                record(
                    "configuration_snapshot.command_set",
                    bool(
                        isinstance(safety, dict)
                        and configuration.get("command_set") == safety.get("command_set")
                    ),
                    "configuration command_set must match manifest safety policy",
                )
                configuration_safety = configuration.get("safety")
                safety_fields = (
                    "query_only",
                    "exact_allowlist",
                    "generic_write_api_exposed",
                    "configuration_controls_exposed",
                    "command_set",
                )
                record(
                    "configuration_snapshot.safety",
                    bool(
                        isinstance(configuration_safety, dict)
                        and isinstance(safety, dict)
                        and all(
                            configuration_safety.get(field) == safety.get(field)
                            for field in safety_fields
                        )
                    ),
                    "configuration safety assertions must match manifest safety policy",
                )
                configuration_capabilities = configuration.get("capabilities")
                manifest_capabilities = manifest.get("capabilities")
                configuration_live_query = (
                    configuration_capabilities.get("live_query")
                    if isinstance(configuration_capabilities, dict)
                    else None
                )
                record(
                    "configuration_snapshot.capabilities",
                    bool(
                        isinstance(configuration_capabilities, dict)
                        and isinstance(manifest_capabilities, dict)
                        and configuration_capabilities.get("diagnostics_supported") is True
                        and configuration_capabilities.get("live_supported")
                        == manifest_capabilities.get("live_supported")
                        and configuration_capabilities.get("live_authorized")
                        == manifest_capabilities.get("live_authorized")
                        and (
                            valid_allowed_queries
                            and configuration_live_query in allowed_queries
                            if manifest_capabilities.get("live_authorized") is True
                            else configuration_live_query is None
                        )
                    ),
                    "configuration capabilities must match manifest and authorized query policy",
                )

                transcript = configuration.get("transcript")
                transcript_is_valid = isinstance(transcript, list) and all(
                    isinstance(item, dict) for item in transcript
                )
                record(
                    "configuration_snapshot.transcript",
                    transcript_is_valid,
                    "transcript must be a list of objects",
                )
                transcript_items = transcript if transcript_is_valid else []
                executed_items = [
                    item
                    for item in transcript_items
                    if item.get("command") is not None and not item.get("skipped")
                ]
                skipped_items = [
                    item for item in transcript_items if item.get("skipped") is True
                ]
                record(
                    "configuration_snapshot.allowlist",
                    bool(
                        valid_allowed_queries
                        and all(
                            item.get("command") in allowed_queries
                            for item in executed_items
                        )
                    ),
                    "every executed configuration command must be in manifest allowed_queries",
                )

                query_plan = configuration.get("query_plan")
                candidate_count = (
                    query_plan.get("candidate_count")
                    if isinstance(query_plan, dict)
                    else None
                )
                record(
                    "configuration_snapshot.query_plan",
                    bool(
                        isinstance(query_plan, dict)
                        and isinstance(candidate_count, int)
                        and candidate_count >= len(executed_items) + len(skipped_items)
                        and query_plan.get("executed_count") == len(executed_items)
                        and query_plan.get("skipped_count") == len(skipped_items)
                    ),
                    "query plan counts must match attempted and skipped transcript entries",
                )

                response_pairs = [
                    (item.get("name"), item.get("response"))
                    for item in executed_items
                    if "response" in item
                ]
                response_names = [name for name, _response in response_pairs]
                values = configuration.get("values")
                record(
                    "configuration_snapshot.responses",
                    bool(
                        isinstance(values, dict)
                        and all(isinstance(name, str) for name in response_names)
                        and len(response_names) == len(set(response_names))
                        and values == dict(response_pairs)
                    ),
                    "values must exactly match acquired transcript responses",
                )
                expected_observed_identity = None
                identity_response = (
                    values.get("identity") if isinstance(values, dict) else None
                )
                if isinstance(identity_response, str):
                    try:
                        parsed_identity = parse_idn(identity_response)
                    except ValueError:
                        pass
                    else:
                        expected_observed_identity = {
                            "raw": parsed_identity.raw,
                            "vendor": parsed_identity.vendor,
                            "model": parsed_identity.model,
                            "serial": parsed_identity.serial,
                            "firmware": parsed_identity.firmware,
                        }
                record(
                    "configuration_snapshot.observed_identity",
                    manifest.get("observed_identity") == expected_observed_identity,
                    "manifest observed_identity must match the parsed identity response",
                )

                diagnostics = configuration.get("diagnostics")
                manifest_readiness = manifest.get("readiness")
                normalized_diagnostics = (
                    dict(diagnostics) if isinstance(diagnostics, dict) else None
                )
                normalized_manifest_readiness = (
                    dict(manifest_readiness)
                    if isinstance(manifest_readiness, dict)
                    else None
                )
                if isinstance(normalized_diagnostics, dict):
                    axes = normalized_diagnostics.get("health_axes")
                    if isinstance(axes, dict):
                        normalized_diagnostics["health_axes"] = {
                            key: value
                            for key, value in axes.items()
                            if key != "evidence_complete"
                        }
                if isinstance(normalized_manifest_readiness, dict):
                    axes = normalized_manifest_readiness.get("health_axes")
                    if isinstance(axes, dict):
                        normalized_manifest_readiness["health_axes"] = {
                            key: value
                            for key, value in axes.items()
                            if key != "evidence_complete"
                        }
                record(
                    "configuration_snapshot.readiness",
                    bool(
                        normalized_diagnostics is not None
                        and normalized_diagnostics == normalized_manifest_readiness
                    ),
                    "configuration diagnostics must match manifest readiness except final evidence status",
                )

                diagnostic_checks = (
                    diagnostics.get("checks") if isinstance(diagnostics, dict) else None
                )
                checks_are_valid = bool(
                    isinstance(diagnostic_checks, list)
                    and diagnostic_checks
                    and all(
                        isinstance(check, dict)
                        and isinstance(check.get("check_id"), str)
                        and bool(check.get("check_id"))
                        and isinstance(check.get("blocks_live"), bool)
                        and check.get("status")
                        in {"PASS", "WARN", "BLOCKED", "UNKNOWN", "N/A"}
                        for check in diagnostic_checks
                    )
                )
                derived_diagnostics_acceptable = bool(
                    checks_are_valid
                    and not any(
                        check.get("blocks_live") is True
                        and check.get("status") != "PASS"
                        for check in diagnostic_checks
                    )
                )
                capabilities = manifest.get("capabilities")
                live_authorized = bool(
                    isinstance(capabilities, dict)
                    and capabilities.get("live_authorized") is True
                )
                expected_can_start_live = (
                    derived_diagnostics_acceptable and live_authorized
                )
                record(
                    "configuration_snapshot.readiness_derived",
                    bool(
                        isinstance(diagnostics, dict)
                        and checks_are_valid
                        and diagnostics.get("diagnostics_acceptable")
                        is derived_diagnostics_acceptable
                        and diagnostics.get("can_start_live")
                        is expected_can_start_live
                    ),
                    "diagnostics_acceptable and can_start_live must agree with blocking checks and authorization",
                )

                live_readiness = configuration.get("live_readiness")
                record(
                    "configuration_snapshot.live_readiness",
                    bool(
                        isinstance(diagnostics, dict)
                        and isinstance(live_readiness, dict)
                        and live_readiness.get("diagnostics_acceptable")
                        is derived_diagnostics_acceptable
                        and live_readiness.get("live_authorized") is live_authorized
                        and live_readiness.get("ready") is expected_can_start_live
                        and live_readiness.get("overall") == diagnostics.get("overall")
                        and live_readiness.get("blockers") == diagnostics.get("blockers")
                        and live_readiness.get("warnings") == diagnostics.get("warnings")
                    ),
                    "live_readiness must mirror diagnostics and manifest authorization",
                )

    try:
        events = _read_jsonl(_artifact_path(root, artifacts.get("events")))
    except (OSError, UnicodeError, ValueError) as exc:
        events = []
        record("events.readable", False, f"{type(exc).__name__}: {exc}")
    else:
        record("events.readable", True, f"{len(events)} JSONL records")
        valid_events = all(
            isinstance(event, dict)
            and event.get("seq") == index
            and event.get("run_id") == run_id
            and _finite_nonnegative(event.get("elapsed_seconds"))
            for index, event in enumerate(events, 1)
        )
        record("events.sequence", valid_events, "event seq/run_id/elapsed must be coherent")
        record(
            "events.run_closed",
            bool(events and isinstance(events[-1], dict) and events[-1].get("event_type") == "run_closed"),
            "the last event must be run_closed",
        )

    streams_value = artifacts.get("streams")
    streams = streams_value if isinstance(streams_value, list) else []
    record("streams.structure", isinstance(streams_value, list), "streams must be a list")
    stream_ids = {
        stream.get("stream_id")
        for stream in streams
        if isinstance(stream, dict) and isinstance(stream.get("stream_id"), str)
    }
    manifest_sample_count = 0
    for index, stream in enumerate(streams):
        prefix = f"streams[{index}]"
        if not isinstance(stream, dict):
            record(f"{prefix}.object", False, "stream entry is not an object")
            continue
        record(
            f"{prefix}.closed",
            stream.get("status") == "closed",
            "stream status must be closed",
        )
        sample_count = stream.get("sample_count")
        valid_count = isinstance(sample_count, int) and sample_count >= 0
        record(
            f"{prefix}.sample_count",
            valid_count,
            "manifest sample_count must be a non-negative integer",
        )
        if not valid_count:
            continue
        manifest_sample_count += sample_count
        csv_name = stream.get("csv")
        try:
            csv_path = _artifact_path(root, csv_name)
        except ValueError as exc:
            record(f"{prefix}.csv", False, str(exc))
            continue
        if not csv_path.is_file():
            expected_absence = sample_count == 0 and stream.get("outcome") == "fault"
            record(
                f"{prefix}.csv",
                expected_absence,
                "CSV may be absent only for a zero-sample fault",
            )
            continue
        try:
            observed_quality = analyze_stream_csv(csv_path)
        except (OSError, UnicodeError, ValueError, csv.Error) as exc:
            record(f"{prefix}.csv", False, f"{type(exc).__name__}: {exc}")
            continue
        record(
            f"{prefix}.sample_count_match",
            observed_quality.get("sample_count") == sample_count,
            (
                f"CSV sample_count {observed_quality.get('sample_count')!r} "
                f"must equal manifest sample_count {sample_count!r}"
            ),
        )
        quality_name = stream.get("quality")
        try:
            persisted_quality = _read_json(_artifact_path(root, quality_name))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            record(f"{prefix}.quality", False, f"{type(exc).__name__}: {exc}")
        else:
            record(
                f"{prefix}.quality",
                persisted_quality == observed_quality,
                "persisted quality must exactly describe the current CSV",
            )

    statistics = manifest.get("statistics")
    record(
        "statistics.sample_count",
        isinstance(statistics, dict)
        and statistics.get("sample_count") == manifest_sample_count,
        f"statistics.sample_count must equal stream total {manifest_sample_count}",
    )

    try:
        interventions = _read_jsonl(_artifact_path(root, artifacts.get("interventions")))
    except (OSError, UnicodeError, ValueError) as exc:
        interventions = []
        record("interventions.readable", False, f"{type(exc).__name__}: {exc}")
    else:
        record("interventions.readable", True, f"{len(interventions)} JSONL records")
        active: dict[object, dict[str, object]] = {}
        completed_by_stream: dict[object, int] = {}
        valid_interventions = True
        for index, record_value in enumerate(interventions, 1):
            if not isinstance(record_value, dict):
                valid_interventions = False
                continue
            intervention_id = record_value.get("intervention_id")
            stream_id = record_value.get("stream_id")
            phase = record_value.get("phase")
            if (
                record_value.get("seq") != index
                or record_value.get("run_id") != run_id
                or stream_id not in stream_ids
                or not _finite_nonnegative(record_value.get("elapsed_seconds"))
            ):
                valid_interventions = False
                continue
            if phase == "start" and intervention_id not in active:
                active[intervention_id] = record_value
            elif phase == "end" and intervention_id in active:
                start = active.pop(intervention_id)
                if (
                    float(record_value["elapsed_seconds"]) < float(start["elapsed_seconds"])
                    or record_value.get("intervention_type") != start.get("intervention_type")
                    or record_value.get("location") != start.get("location")
                ):
                    valid_interventions = False
                completed_by_stream[stream_id] = completed_by_stream.get(stream_id, 0) + 1
            else:
                valid_interventions = False
        if active:
            valid_interventions = False
        record(
            "interventions.paired",
            valid_interventions,
            "interventions must be ordered, paired start/end records",
        )
        for index, stream in enumerate(streams):
            if isinstance(stream, dict):
                stream_id = stream.get("stream_id")
                record(
                    f"streams[{index}].intervention_count",
                    stream.get("intervention_count", 0)
                    == completed_by_stream.get(stream_id, 0),
                    "stream intervention_count must equal completed pairs",
                )
        record(
            "statistics.intervention_count",
            isinstance(statistics, dict)
            and statistics.get("intervention_count") == sum(completed_by_stream.values()),
            "statistics.intervention_count must equal completed pairs",
        )

    return {
        "schema_version": 1,
        "run_id": run_id,
        "passed": not errors,
        "checks": checks,
        "errors": errors,
    }


def write_verification_report(
    run_directory: Path,
    report: dict[str, object],
) -> Path:
    path = Path(run_directory) / "evidence-verification.json"
    temporary = path.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path
