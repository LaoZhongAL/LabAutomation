"""Host-side quality metrics for a completed four-column readout CSV."""

from __future__ import annotations

import csv
import math
import statistics
from pathlib import Path


CSV_FIELDS = (
    "elapsed_seconds",
    "voltage_v",
    "raw_response",
    "query_elapsed_ms",
)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def _finite_number(
    row: dict[str, str],
    field: str,
    row_number: int,
    *,
    nonnegative: bool = False,
) -> float:
    try:
        value = float(row[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"row {row_number} field {field!r} is not numeric: {row.get(field)!r}"
        ) from exc
    if not math.isfinite(value):
        raise ValueError(f"row {row_number} field {field!r} is non-finite")
    if nonnegative and value < 0:
        raise ValueError(f"row {row_number} field {field!r} is negative")
    return value


def analyze_stream_csv(csv_path: Path) -> dict[str, object]:
    """Return descriptive metrics without judging sample/device suitability."""

    path = Path(csv_path)
    elapsed_values: list[float] = []
    voltages: list[float] = []
    raw_responses: list[str] = []
    latencies_ms: list[float] = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CSV_FIELDS:
            raise ValueError(
                f"CSV fields must be exactly {CSV_FIELDS!r}; got {reader.fieldnames!r}"
            )
        for row_number, row in enumerate(reader, start=2):
            if set(row) != set(CSV_FIELDS) or any(row[name] is None for name in CSV_FIELDS):
                raise ValueError(f"row {row_number} does not match the four-column CSV schema")
            elapsed = _finite_number(
                row,
                "elapsed_seconds",
                row_number,
                nonnegative=True,
            )
            if elapsed_values and elapsed < elapsed_values[-1]:
                raise ValueError(f"row {row_number} elapsed_seconds is not monotonic")
            raw = row["raw_response"]
            if not raw.strip():
                raise ValueError(f"row {row_number} raw_response is empty")
            elapsed_values.append(elapsed)
            voltages.append(_finite_number(row, "voltage_v", row_number))
            raw_responses.append(raw)
            latencies_ms.append(
                _finite_number(
                    row,
                    "query_elapsed_ms",
                    row_number,
                    nonnegative=True,
                )
            )

    intervals = [
        current - previous
        for previous, current in zip(elapsed_values, elapsed_values[1:])
    ]
    duplicate_count = sum(
        current == previous
        for previous, current in zip(raw_responses, raw_responses[1:])
    )
    longest_run = 0
    current_run = 0
    previous_raw: str | None = None
    for raw in raw_responses:
        current_run = current_run + 1 if raw == previous_raw else 1
        longest_run = max(longest_run, current_run)
        previous_raw = raw

    voltage_median = statistics.median(voltages) if voltages else None
    mad = (
        statistics.median(abs(value - voltage_median) for value in voltages)
        if voltage_median is not None
        else None
    )
    drift = None
    if len(voltages) >= 2:
        elapsed_mean = statistics.fmean(elapsed_values)
        voltage_mean = statistics.fmean(voltages)
        denominator = sum((value - elapsed_mean) ** 2 for value in elapsed_values)
        if denominator > 0:
            drift = sum(
                (elapsed - elapsed_mean) * (voltage - voltage_mean)
                for elapsed, voltage in zip(elapsed_values, voltages)
            ) / denominator

    return {
        "schema_version": 1,
        "analysis_scope": "host_csv_observation_only_no_thresholds",
        "source_csv": path.name,
        "sample_count": len(voltages),
        "elapsed": {
            "first_seconds": elapsed_values[0] if elapsed_values else None,
            "last_seconds": elapsed_values[-1] if elapsed_values else None,
            "span_seconds": (
                elapsed_values[-1] - elapsed_values[0] if elapsed_values else None
            ),
            "interval_seconds": _distribution(intervals),
        },
        "query_latency_ms": _distribution(latencies_ms),
        "voltage_v": {
            "median": voltage_median,
            "mad": mad,
            "standard_deviation": (
                statistics.pstdev(voltages) if voltages else None
            ),
            "peak_to_peak": max(voltages) - min(voltages) if voltages else None,
            "linear_drift_v_per_s": drift,
        },
        "raw_response": {
            "consecutive_duplicate_count": duplicate_count,
            "consecutive_duplicate_fraction": (
                duplicate_count / (len(raw_responses) - 1)
                if len(raw_responses) >= 2
                else None
            ),
            "longest_identical_run": longest_run,
        },
    }
