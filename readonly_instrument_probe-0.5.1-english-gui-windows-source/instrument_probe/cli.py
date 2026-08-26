from __future__ import annotations

import argparse
import json
from pathlib import Path

from .catalog import PROFILES, queries_for
from .collector import collect, host_environment
from .lab_setup import (
    LAB_INSTRUMENTS,
    assignment_for_resource,
    assignment_dicts,
    detect_model,
    validate_confirmed_target,
)
from .production import production_host_readiness, require_production_host
from .transports import (
    DryRunTransport,
    PyVisaQueryTransport,
    SimulatedTransport,
    list_visa_resources,
    query_identity_only,
)


REAL_ACK = "QUERY_ONLY"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only inventory probe for Keithley 2182A, 6221, and 2450."
    )
    parser.add_argument("--model", choices=sorted(PROFILES))
    parser.add_argument("--mode", choices=("simulate", "dry-run", "real"), default="simulate")
    parser.add_argument("--scope", choices=("identity", "core", "full"), default="core")
    parser.add_argument("--resource", help="VISA resource; required only for --mode real")
    parser.add_argument("--visa-library", help="Optional PyVISA library selector")
    parser.add_argument("--timeout-ms", type=int, default=3000)
    parser.add_argument(
        "--real-device-ack",
        metavar="QUERY_ONLY",
        help="Required literal confirmation for real mode",
    )
    parser.add_argument(
        "--2450-command-set-ack",
        choices=("SCPI", "TSP"),
        help="For 2450 real core/full only: confirm the command set observed on that instrument",
    )
    parser.add_argument("--output", type=Path, help="Write JSON snapshot to this local file")
    parser.add_argument(
        "--overwrite-output",
        action="store_true",
        help="Allow replacing an offline output file; forbidden for real VISA actions",
    )
    parser.add_argument("--show-plan", action="store_true", help="Print allowlisted queries and exit")
    parser.add_argument(
        "--list-lab-addresses",
        action="store_true",
        help="Print NI MAX confirmed laboratory GPIB assignments without opening VISA",
    )
    parser.add_argument(
        "--list-visa-resources",
        action="store_true",
        help="Enumerate VISA resources; does not open or message instruments",
    )
    parser.add_argument(
        "--identify-lab",
        action="store_true",
        help="Send exactly *IDN? to each NI MAX confirmed laboratory instrument",
    )
    parser.add_argument(
        "--audit-host",
        action="store_true",
        help="Report whether this computer satisfies the laboratory real-mode gate",
    )
    return parser


def _render(data: object, output: Path | None, *, overwrite: bool = False) -> None:
    rendered = json.dumps(data, indent=2, ensure_ascii=False)
    if output:
        if output.exists() and not overwrite:
            raise SystemExit(
                f"output already exists and was not replaced: {output}. "
                "Use a new timestamped production run directory."
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def _require_production_evidence(args: argparse.Namespace, operation: str) -> None:
    if not args.output:
        raise SystemExit(f"{operation} requires --output so the production evidence is preserved")
    if args.overwrite_output:
        raise SystemExit(f"{operation} forbids --overwrite-output; use a new output filename")
    if args.output.exists():
        raise SystemExit(
            f"output already exists, so {operation} was not started: {args.output}. "
            "Use a new timestamped production run directory."
        )
    try:
        require_production_host()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


def _identify_lab(args: argparse.Namespace) -> int:
    if args.mode != "real":
        raise SystemExit("--identify-lab requires --mode real")
    if args.real_device_ack != REAL_ACK:
        raise SystemExit("--identify-lab requires --real-device-ack QUERY_ONLY")
    _require_production_evidence(args, "--identify-lab")

    rows: list[dict[str, object]] = []
    for assignment in LAB_INSTRUMENTS:
        row = assignment.as_dict()
        try:
            identity = query_identity_only(
                assignment.resource,
                visa_library=args.visa_library,
                timeout_ms=args.timeout_ms,
            )
            detected = detect_model(identity)
            row.update({
                "ok": True,
                "identity": identity,
                "detected_model": detected,
                "matches_expected_model": detected == assignment.expected_model,
            })
        except Exception as exc:
            row.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        rows.append(row)

    all_identities_match = all(
        row.get("ok") is True and row.get("matches_expected_model") is True
        for row in rows
    )
    report = {
        "operation": "identity-only confirmed laboratory map verification",
        "address_source": "NI MAX screenshot confirmed on 2026-08-19",
        "query_sent_per_instrument": "*IDN?",
        "all_six_identities_match": all_identities_match,
        "host_environment": host_environment(),
        "production_readiness": production_host_readiness(),
        "assignments": rows,
    }
    _render(report, args.output)
    return 0 if all_identities_match else 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.audit_host:
        report = {
            "operation": "production host audit",
            "host_environment": host_environment(),
            "production_readiness": production_host_readiness(),
        }
        _render(report, args.output, overwrite=args.overwrite_output)
        return 0 if report["production_readiness"]["host_gate_passed"] else 2

    if args.list_lab_addresses:
        _render(
            {
                "status": "confirmed_in_ni_max",
                "confirmed_on": "2026-08-19",
                "assignments": assignment_dicts(),
            },
            args.output,
            overwrite=args.overwrite_output,
        )
        return 0

    if args.list_visa_resources:
        _require_production_evidence(args, "--list-visa-resources")
        resources = list_visa_resources(args.visa_library)
        _render({
            "operation": "VISA resource enumeration",
            "host_environment": host_environment(),
            "production_readiness": production_host_readiness(),
            "resources": resources,
        }, args.output)
        return 0

    if args.identify_lab:
        return _identify_lab(args)

    if not args.model:
        raise SystemExit("--model is required unless using a list/identify action")
    profile = PROFILES[args.model]

    if args.show_plan:
        print(json.dumps([
            {"name": x.name, "command": x.command, "group": x.group, "scope": x.scope}
            for x in queries_for(profile, args.scope)
        ], indent=2, ensure_ascii=False))
        return 0

    if args.mode == "simulate":
        transport = SimulatedTransport(profile)
    elif args.mode == "dry-run":
        transport = DryRunTransport(profile)
    else:
        if not args.resource:
            raise SystemExit("real mode requires --resource")
        if args.real_device_ack != REAL_ACK:
            raise SystemExit("real mode requires --real-device-ack QUERY_ONLY")
        try:
            validate_confirmed_target(profile.key, args.resource, args.scope)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if profile.key.startswith("2450-") and args.scope != "identity":
            if getattr(args, "2450_command_set_ack") != profile.command_set:
                raise SystemExit(
                    f"2450 {args.scope} requires --2450-command-set-ack "
                    f"{profile.command_set}; confirm it on this instrument before continuing"
                )
        _require_production_evidence(args, "real mode")
        transport = PyVisaQueryTransport(
            profile,
            args.resource,
            visa_library=args.visa_library,
            timeout_ms=args.timeout_ms,
        )

    try:
        report = collect(profile, transport, args.scope)
    finally:
        transport.close()

    if args.mode == "real":
        report["production_readiness"] = production_host_readiness()
        assignment = assignment_for_resource(args.resource)
        report["confirmed_lab_assignment"] = (
            assignment.as_dict() if assignment is not None else None
        )

    _render(report, args.output, overwrite=args.overwrite_output)
    unsafe_stop = (
        report["safety"]["stopped_after_first_io_error"]
        or report["safety"]["stopped_after_identity_mismatch"]
    )
    return 0 if not unsafe_stop else 2
