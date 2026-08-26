from __future__ import annotations

import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import gpib6_2182a_monitor as monitor
from diagnostic_core import DiagnosticState, identity_is_exact
from instrument_inventory import SimulatedInstrument


class MultiInstrumentCollectionTests(unittest.TestCase):
    def test_all_five_targets_use_exact_identity_and_separate_capabilities(self):
        inventory = monitor.build_simulated_inventory()
        self.assertEqual(len(inventory.entries), 5)

        for entry in inventory.entries:
            with self.subTest(resource=entry.resource):
                target = monitor.target_from_inventory_entry(entry)
                self.assertIsNotNone(target)
                assert target is not None
                report = monitor.collect_configuration(
                    "simulate",
                    target=target,
                )
                self.assertEqual(report["target_key"], target.key)
                self.assertEqual(report["resource"], target.resource)
                self.assertTrue(
                    identity_is_exact(
                        report["values"]["identity"],
                        monitor.core_target_for(target),
                    )
                )
                self.assertTrue(report["diagnostics"]["diagnostics_acceptable"])
                self.assertEqual(
                    report["diagnostics"]["can_start_live"],
                    target.key == monitor.DEFAULT_TARGET_KEY,
                )
                self.assertEqual(
                    report["capabilities"]["live_supported"],
                    target.key == monitor.DEFAULT_TARGET_KEY,
                )

    def test_future_2182a_reuses_profile_but_never_inherits_live_capability(self):
        inventory = monitor.build_simulated_inventory(
            (
                SimulatedInstrument(
                    "GPIB0::11::INSTR",
                    "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,NEW2182,C11/A03",
                ),
            )
        )

        target = monitor.target_from_inventory_entry(inventory.entries[0])

        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.profile_key, "2182a")
        self.assertEqual(target.resource, "GPIB0::11::INSTR")
        self.assertEqual(target.serial, "NEW2182")
        self.assertFalse(target.live_supported)
        self.assertNotIn("FETCh?", monitor.allowed_queries_for_target(target))

    def test_forged_live_flag_does_not_authorize_another_resource(self):
        approved = monitor.target_for(monitor.DEFAULT_TARGET_KEY)
        forged = monitor.InstrumentTarget(
            key=approved.key,
            label=approved.label,
            resource="GPIB0::7::INSTR",
            vendor=approved.vendor,
            model=approved.model,
            serial=approved.serial,
            firmware=approved.firmware,
            profile_key=approved.profile_key,
            live_supported=True,
        )

        report = monitor.collect_configuration("simulate", target=forged)

        self.assertNotIn("FETCh?", monitor.allowed_queries_for_target(forged))
        self.assertTrue(report["capabilities"]["live_supported"])
        self.assertFalse(report["capabilities"]["live_authorized"])
        self.assertFalse(report["diagnostics"]["can_start_live"])
        self.assertFalse(report["live_readiness"]["ready"])

    def test_unknown_and_malformed_inventory_entries_do_not_create_targets(self):
        inventory = monitor.build_simulated_inventory(
            (
                SimulatedInstrument(
                    "GPIB0::11::INSTR",
                    "KEITHLEY INSTRUMENTS INC.,MODEL 2400,UNKNOWN2400,1.0",
                ),
                SimulatedInstrument("GPIB0::12::INSTR", "not-a-valid-idn"),
            )
        )

        self.assertEqual(
            [entry.status for entry in inventory.entries],
            ["unknown_model", "malformed_identity"],
        )
        self.assertEqual(
            [monitor.target_from_inventory_entry(entry) for entry in inventory.entries],
            [None, None],
        )

    def test_2450_executes_only_active_compliance_branch(self):
        context = monitor.SimulationContext(
            "nominal",
            monitor.allowed_queries_for_target("2450-gpib25"),
        )
        report = monitor.collect_configuration(
            "simulate",
            context,
            target_key="2450-gpib25",
        )
        history = [item["command"] for item in context.query_history]

        self.assertIn("print(smu.source.ilimit.level)", history)
        self.assertIn("print(smu.source.ilimit.tripped)", history)
        self.assertNotIn("print(smu.source.vlimit.level)", history)
        self.assertNotIn("print(smu.source.vlimit.tripped)", history)
        self.assertEqual(report["query_plan"]["skipped_count"], 2)
        skipped = {item["name"] for item in report["transcript"] if item.get("skipped")}
        self.assertEqual(
            skipped,
            {"source_voltage_limit_v", "voltage_limit_tripped"},
        )

    def test_6221_output_on_blocks_safe_idle_without_a_write(self):
        values = monitor.SIMULATED_VALUES_BY_TARGET["6221-gpib9"]
        with mock.patch.dict(values, {"OUTP?": "1"}):
            report = monitor.collect_configuration(
                "simulate",
                target_key="6221-gpib9",
            )

        self.assertFalse(report["diagnostics"]["diagnostics_acceptable"])
        self.assertFalse(report["diagnostics"]["can_start_live"])
        self.assertTrue(
            any(
                check["check_id"] == "safety.output_off"
                and check["status"] == "BLOCKED"
                for check in report["diagnostics"]["checks"]
            )
        )

    def test_generic_2182a_invalid_data_format_blocks_diagnostics(self):
        values = monitor.SIMULATED_VALUES_BY_TARGET["2182a-gpib7"]
        with mock.patch.dict(values, {"FORM:DATA?": "garbage"}):
            report = monitor.collect_configuration(
                "simulate",
                target_key="2182a-gpib7",
            )

        self.assertFalse(report["diagnostics"]["diagnostics_acceptable"])
        self.assertTrue(
            any(
                check["check_id"] == "configuration.data_format"
                and check["status"] == "BLOCKED"
                for check in report["diagnostics"]["checks"]
            )
        )

    def test_generic_2182a_invalid_inactive_channel_warns_without_blocking(self):
        values = monitor.SIMULATED_VALUES_BY_TARGET["2182a-gpib7"]
        with mock.patch.dict(values, {"SENS:VOLT:DC:CHAN2:RANG?": "garbage"}):
            report = monitor.collect_configuration(
                "simulate",
                target_key="2182a-gpib7",
            )

        self.assertTrue(report["diagnostics"]["diagnostics_acceptable"])
        inactive = next(
            check
            for check in report["diagnostics"]["checks"]
            if check["check_id"] == "configuration.ch2_range_v"
        )
        self.assertEqual(inactive["status"], "WARN")
        self.assertFalse(inactive["blocks_live"])

    def test_exact_gpib6_accepts_valid_nonpreset_2182a_settings(self):
        values = monitor.SIMULATED_VALUES_BY_TARGET["2182a-gpib6"]
        nonpreset = {
            "SYST:LFREQUENCY?": "60",
            "SENS:CHAN?": "2",
            "SENS:VOLT:DC:NPLC?": "10",
            "SENS:VOLT:DC:CHAN1:RANG?": "0.1",
            "SENS:VOLT:DC:CHAN1:RANG:AUTO?": "1",
            "SENS:VOLT:DC:CHAN1:DFILTER?": "1",
            "SENS:VOLT:DC:CHAN1:LPASS?": "1",
            "SENS:VOLT:DC:CHAN2:RANG?": "0.001",
            "SENS:VOLT:DC:CHAN2:RANG:AUTO?": "0",
            "SENS:VOLT:DC:CHAN2:DFILTER?": "0",
            "SENS:VOLT:DC:CHAN2:LPASS?": "1",
            "TRIG:COUNT?": "7",
            "TRIG:DELAY?": "0.25",
            "TRIG:SOURCE?": "EXT",
        }
        with mock.patch.dict(values, nonpreset):
            report = monitor.collect_configuration(
                "simulate",
                target_key="2182a-gpib6",
            )

        self.assertTrue(report["diagnostics"]["diagnostics_acceptable"])
        self.assertTrue(report["diagnostics"]["can_start_live"])
        self.assertEqual(report["values"]["active_channel"], "2")
        compatibility = {
            check["check_id"]: check["status"]
            for check in report["diagnostics"]["checks"]
            if check["check_id"].startswith("live_compatibility.")
        }
        self.assertEqual(set(compatibility.values()), {"PASS"})

    def test_6221_invalid_output_response_blocks_diagnostics(self):
        values = monitor.SIMULATED_VALUES_BY_TARGET["6221-gpib9"]
        with mock.patch.dict(values, {"OUTP:RESPONSE?": "garbage"}):
            report = monitor.collect_configuration(
                "simulate",
                target_key="6221-gpib9",
            )

        self.assertFalse(report["diagnostics"]["diagnostics_acceptable"])
        self.assertTrue(
            any(
                check["check_id"] == "configuration.output_response"
                and check["status"] == "BLOCKED"
                for check in report["diagnostics"]["checks"]
            )
        )

    def test_2450_non_numeric_source_level_blocks_diagnostics(self):
        values = monitor.SIMULATED_VALUES_BY_TARGET["2450-gpib25"]
        with mock.patch.dict(
            values,
            {"print(smu.source.level)": "garbage"},
        ):
            report = monitor.collect_configuration(
                "simulate",
                target_key="2450-gpib25",
            )

        self.assertFalse(report["diagnostics"]["diagnostics_acceptable"])
        self.assertTrue(
            any(
                check["check_id"] == "configuration.source_level"
                and check["status"] == "BLOCKED"
                for check in report["diagnostics"]["checks"]
            )
        )

    def test_6221_interlock_state_is_decoded_without_blocking_output_off(self):
        report = monitor.collect_configuration(
            "simulate",
            target_key="6221-gpib9",
        )

        interlock = next(
            check
            for check in report["diagnostics"]["checks"]
            if check["check_id"] == "safety.interlock_ready"
        )
        self.assertEqual(interlock["status"], "WARN")
        self.assertFalse(interlock["blocks_live"])
        self.assertTrue(report["diagnostics"]["diagnostics_acceptable"])

    def test_6221_operation_idle_bit_is_required_for_safe_idle(self):
        values = monitor.SIMULATED_VALUES_BY_TARGET["6221-gpib9"]
        with mock.patch.dict(values, {"STAT:OPER:COND?": "0"}):
            report = monitor.collect_configuration(
                "simulate",
                target_key="6221-gpib9",
            )

        operation_idle = next(
            check
            for check in report["diagnostics"]["checks"]
            if check["check_id"] == "safety.operation_idle"
        )
        self.assertEqual(operation_idle["status"], "BLOCKED")
        self.assertFalse(report["diagnostics"]["diagnostics_acceptable"])
        self.assertEqual(report["diagnostics"]["health_axes"]["safe_idle"], "BLOCKED")

    def test_calibration_condition_never_claims_external_traceability(self):
        expected_condition = {
            "2182a-gpib7": "PASS",
            "6221-gpib9": "PASS",
            "2450-gpib25": "UNKNOWN",
        }
        for target_key, condition_status in expected_condition.items():
            with self.subTest(target=target_key):
                report = monitor.collect_configuration(
                    "simulate",
                    target_key=target_key,
                )
                axes = report["diagnostics"]["health_axes"]
                self.assertEqual(axes["calibration_condition"], condition_status)
                self.assertEqual(axes["calibration_traceability"], "UNKNOWN")
                self.assertNotIn("calibration_known", axes)

    def test_6221_unknown_interlock_state_blocks_diagnostics(self):
        values = monitor.SIMULATED_VALUES_BY_TARGET["6221-gpib9"]
        with mock.patch.dict(values, {"OUTP:INTERLOCK:TRIPPED?": "UNKNOWN"}):
            report = monitor.collect_configuration(
                "simulate",
                target_key="6221-gpib9",
            )

        self.assertFalse(report["diagnostics"]["diagnostics_acceptable"])
        self.assertTrue(
            any(
                check["check_id"] == "safety.interlock_ready"
                and check["status"] == "BLOCKED"
                for check in report["diagnostics"]["checks"]
            )
        )

    def test_2450_active_limit_blocks_readiness_without_changing_output(self):
        values = monitor.SIMULATED_VALUES_BY_TARGET["2450-gpib25"]
        with mock.patch.dict(
            values,
            {"print(smu.source.ilimit.tripped)": "1"},
        ):
            report = monitor.collect_configuration(
                "simulate",
                target_key="2450-gpib25",
            )

        self.assertFalse(report["diagnostics"]["diagnostics_acceptable"])
        self.assertEqual(report["values"]["source_output"], "0")
        self.assertTrue(
            any(
                check["check_id"] == "safety.active_limit_not_reached"
                and check["status"] == "BLOCKED"
                for check in report["diagnostics"]["checks"]
            )
        )

    def test_2450_asserted_interlock_is_valid_while_output_is_off(self):
        values = monitor.SIMULATED_VALUES_BY_TARGET["2450-gpib25"]
        with mock.patch.dict(values, {"print(smu.interlock.tripped)": "1"}):
            report = monitor.collect_configuration(
                "simulate",
                target_key="2450-gpib25",
            )

        interlock = next(
            check
            for check in report["diagnostics"]["checks"]
            if check["check_id"] == "safety.interlock_assertion_state"
        )
        self.assertEqual(interlock["status"], "PASS")
        self.assertTrue(report["diagnostics"]["diagnostics_acceptable"])

    def test_2450_invalid_interlock_assertion_state_blocks_diagnostics(self):
        values = monitor.SIMULATED_VALUES_BY_TARGET["2450-gpib25"]
        with mock.patch.dict(values, {"print(smu.interlock.tripped)": "2"}):
            report = monitor.collect_configuration(
                "simulate",
                target_key="2450-gpib25",
            )

        interlock = next(
            check
            for check in report["diagnostics"]["checks"]
            if check["check_id"] == "safety.interlock_assertion_state"
        )
        self.assertEqual(interlock["status"], "BLOCKED")
        self.assertFalse(report["diagnostics"]["diagnostics_acceptable"])

    def test_2450_unknown_source_function_blocks_readiness(self):
        values = monitor.SIMULATED_VALUES_BY_TARGET["2450-gpib25"]
        with mock.patch.dict(
            values,
            {"print(smu.source.func)": "UNSUPPORTED_CURRENT_MODE"},
        ):
            report = monitor.collect_configuration(
                "simulate",
                target_key="2450-gpib25",
            )

        skipped = {item["name"] for item in report["transcript"] if item.get("skipped")}
        self.assertEqual(
            skipped,
            {
                "source_current_limit_a",
                "current_limit_tripped",
                "source_voltage_limit_v",
                "voltage_limit_tripped",
            },
        )
        self.assertFalse(report["diagnostics"]["diagnostics_acceptable"])
        self.assertEqual(report["diagnostics"]["overall"], "BLOCKED")
        self.assertTrue(
            any(
                check["check_id"] == "configuration.source_function"
                and check["status"] == "BLOCKED"
                for check in report["diagnostics"]["checks"]
            )
        )

    def test_model_specific_fault_scenarios_run_end_to_end(self):
        cases = (
            ("6221-gpib9", "6221_output_on", "OUTP?", "safety.output_off"),
            (
                "6221-gpib9",
                "6221_interlock_invalid",
                "OUTP:INTERLOCK:TRIPPED?",
                "safety.interlock_ready",
            ),
            (
                "6221-gpib9",
                "6221_over_temperature",
                "STAT:MEAS:COND?",
                "safety.over_temperature",
            ),
            (
                "6221-gpib9",
                "6221_compliance_active",
                "STAT:MEAS:COND?",
                "safety.compliance_active",
            ),
            (
                "6221-gpib9",
                "6221_calibration_questionable",
                "STAT:QUES:COND?",
                "calibration.condition.questionable",
            ),
            (
                "6221-gpib9",
                "6221_invalid_response",
                "OUTP:RESPONSE?",
                "configuration.output_response",
            ),
            (
                "6221-gpib9",
                "6221_configuration_timeout",
                "STAT:OPER:COND?",
                "communication.operation_condition",
            ),
            (
                "2450-gpib25",
                "2450_output_on",
                "print(smu.source.output)",
                "safety.output_off",
            ),
            (
                "2450-gpib25",
                "2450_invalid_source_mode",
                "print(smu.source.func)",
                "configuration.source_function",
            ),
            (
                "2450-gpib25",
                "2450_active_limit",
                "print(smu.source.ilimit.tripped)",
                "safety.active_limit_not_reached",
            ),
            (
                "2450-gpib25",
                "2450_protection_tripped",
                "print(smu.source.protect.tripped)",
                "safety.protection_not_tripped",
            ),
            (
                "2450-gpib25",
                "2450_interlock_invalid",
                "print(smu.interlock.tripped)",
                "safety.interlock_assertion_state",
            ),
            (
                "2450-gpib25",
                "2450_invalid_response",
                "print(smu.source.level)",
                "configuration.source_level",
            ),
            (
                "2450-gpib25",
                "2450_configuration_timeout",
                "print(smu.source.level)",
                "communication.source_level",
            ),
        )

        for target_key, scenario, command, check_id in cases:
            with self.subTest(target=target_key, scenario=scenario):
                allowed_queries = monitor.allowed_queries_for_target(target_key)
                self.assertIn(scenario, monitor.fault_scenarios_for_target(target_key))
                context = monitor.SimulationContext(scenario, allowed_queries)
                report = monitor.collect_configuration(
                    "simulate",
                    context,
                    target_key=target_key,
                )

                check = next(
                    item
                    for item in report["diagnostics"]["checks"]
                    if item["check_id"] == check_id
                )
                self.assertEqual(check["status"], "BLOCKED")
                self.assertFalse(report["diagnostics"]["diagnostics_acceptable"])
                self.assertIn(command, [item["command"] for item in context.query_history])
                self.assertEqual(len(context.consumed_rule_ids), 1)
                self.assertTrue(
                    all(
                        item["command"] in allowed_queries
                        for item in context.query_history
                    )
                )

    def test_model_specific_fault_scenarios_are_not_cross_offered(self):
        gpib6_scenarios = monitor.fault_scenarios_for_target("2182a-gpib6")
        source_scenarios = monitor.fault_scenarios_for_target("6221-gpib9")
        smu_scenarios = monitor.fault_scenarios_for_target("2450-gpib25")

        self.assertFalse(any(name.startswith("6221_") for name in gpib6_scenarios))
        self.assertFalse(any(name.startswith("2450_") for name in gpib6_scenarios))
        self.assertFalse(any(name.startswith("2450_") for name in source_scenarios))
        self.assertFalse(any(name.startswith("6221_") for name in smu_scenarios))


class MultiInstrumentWorkerTests(unittest.TestCase):
    @staticmethod
    def _owner(kind, mode, *, target=None, snapshot_id=None):
        return monitor.OperationOwner(
            operation_id=f"test-{kind}",
            kind=kind,
            mode=mode,
            target_key=target.key if target is not None else None,
            inventory_snapshot_id=snapshot_id,
        )

    @staticmethod
    def _worker_app():
        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.events = queue.Queue()
        app.stop_event = threading.Event()
        return app

    def test_worker_freezes_runtime_target_snapshot_and_exact_query_allowlist(self):
        app = self._worker_app()
        inventory = monitor.build_simulated_inventory()
        entry = next(
            item for item in inventory.entries if item.resource == "GPIB0::9::INSTR"
        )
        target = monitor.target_from_inventory_entry(entry)
        self.assertIsNotNone(target)
        assert target is not None
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            inventory_file = output_root / "inventory-snapshot.json"
            monitor.write_json_atomic(inventory_file, inventory.as_dict())
            owner = self._owner(
                "diagnostic",
                "simulate",
                target=target,
                snapshot_id=inventory.snapshot_id,
            )
            app._configuration_worker(
                owner,
                "simulate",
                "nominal",
                output_root,
                target,
                inventory.snapshot_id,
                inventory,
                inventory_file,
            )
            queued_owner, event, payload = app.events.get_nowait()
            self.assertIs(queued_owner, owner)
            self.assertEqual(event, "configuration")
            report, run_directory, _snapshot, journal, context = payload
            manifest = json.loads(
                (run_directory / "run-manifest.json").read_text(encoding="utf-8")
            )
            reference = json.loads(
                (run_directory / "inventory-snapshot-reference.json").read_text(
                    encoding="utf-8"
                )
            )
            journal.finalize("test_complete")

        self.assertEqual(journal.state, DiagnosticState.OBSERVE_READY)
        self.assertEqual(report["target_key"], target.key)
        self.assertEqual(report["inventory_snapshot_id"], inventory.snapshot_id)
        self.assertEqual(manifest["target"]["resource"], "GPIB0::9::INSTR")
        self.assertEqual(manifest["target"]["serial"], "4533811")
        self.assertEqual(
            manifest["target"]["inventory_snapshot_id"],
            inventory.snapshot_id,
        )
        self.assertEqual(
            manifest["target"]["inventory_snapshot_reference"],
            "inventory-snapshot-reference.json",
        )
        self.assertEqual(reference["inventory_snapshot_id"], inventory.snapshot_id)
        self.assertEqual(reference["snapshot"], inventory.as_dict())
        self.assertEqual(
            reference["snapshot_payload_sha256"],
            monitor.json_payload_sha256(inventory.as_dict()),
        )
        self.assertEqual(
            manifest["target"]["inventory_snapshot_payload_sha256"],
            reference["snapshot_payload_sha256"],
        )
        self.assertEqual(
            manifest["target"]["inventory_source_file_sha256"],
            reference["source_file_sha256"],
        )
        self.assertEqual(manifest["safety"]["command_set"], "SCPI")
        self.assertFalse(manifest["capabilities"]["live_supported"])
        allowed_queries = set(manifest["safety"]["allowed_queries"])
        self.assertEqual(
            allowed_queries,
            set(monitor.allowed_queries_for_target(target)),
        )
        self.assertNotIn("FETCh?", allowed_queries)
        self.assertTrue(
            all(
                item["command"] in allowed_queries
                for item in context.query_history
            )
        )
        for forbidden in (
            "READ?",
            "*ESR?",
            "SYST:ERR?",
            "STAT:OPER:EVEN?",
            "*RST",
            "ABOR",
            "INIT",
            "TRAC:CLE",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, allowed_queries)

    def test_diagnostic_only_profile_is_rejected_before_live_session(self):
        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.worker = None
        app.recording_fault_latched = False
        app.stream_had_error = False
        app.configuration = {
            "target_key": "6221-gpib9",
            "capabilities": {"live_supported": False},
            "diagnostics": {
                "diagnostics_acceptable": True,
                "can_start_live": False,
            },
        }
        app.run_directory = Path("unused")
        app.diagnostic_run = mock.Mock(state=DiagnosticState.OBSERVE_READY)
        app.diagnostic_target = monitor.target_for("6221-gpib9")
        app.messagebox = mock.Mock()

        with mock.patch.object(
            monitor,
            "session_factory",
            side_effect=AssertionError("diagnostic-only profile must not open Live session"),
        ):
            app._start_stream()

        app.messagebox.showerror.assert_called_once()

    def test_inventory_preflight_failure_never_starts_real_scan(self):
        app = self._worker_app()
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            monitor,
            "write_json_atomic",
            side_effect=OSError("plan write failed"),
        ), mock.patch.object(monitor, "refresh_inventory") as refresh:
            owner = self._owner("inventory", "real")
            app._inventory_worker(owner, "real", Path(temp_dir))

        refresh.assert_not_called()
        queued_owner, event, payload = app.events.get_nowait()
        self.assertIs(queued_owner, owner)
        self.assertEqual(event, "inventory_error")
        message, phase, _run_directory, _plan_file, snapshot = payload
        self.assertEqual(phase, "preflight")
        self.assertIsNone(snapshot)
        self.assertIn("plan write failed", message)

    def test_inventory_persist_failure_reports_that_scan_completed(self):
        app = self._worker_app()
        inventory = monitor.build_simulated_inventory()
        original_writer = monitor.write_json_atomic
        writes = []

        def fail_second_write(path, payload):
            writes.append(path)
            if len(writes) == 2:
                raise OSError("snapshot persist failed")
            return original_writer(path, payload)

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            monitor,
            "write_json_atomic",
            side_effect=fail_second_write,
        ), mock.patch.object(
            monitor,
            "refresh_inventory",
            return_value=inventory,
        ) as refresh:
            owner = self._owner("inventory", "real")
            app._inventory_worker(owner, "real", Path(temp_dir))
            queued_owner, event, payload = app.events.get_nowait()
            self.assertIs(queued_owner, owner)
            message, phase, _run_directory, plan_file, snapshot = payload
            plan_existed = plan_file.is_file()

        refresh.assert_called_once_with()
        self.assertEqual(event, "inventory_error")
        self.assertEqual(phase, "persist")
        self.assertIs(snapshot, inventory)
        self.assertTrue(plan_existed)
        self.assertIn("snapshot persist failed", message)

    def test_tampered_inventory_source_blocks_before_configuration_query(self):
        app = self._worker_app()
        inventory = monitor.build_simulated_inventory()
        target = monitor.target_from_inventory_entry(inventory.entries[0])
        self.assertIsNotNone(target)
        assert target is not None

        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            source_file = output_root / "inventory-snapshot.json"
            tampered = inventory.as_dict()
            tampered["counts"]["recognized_profile_count"] = 999
            monitor.write_json_atomic(source_file, tampered)
            with mock.patch.object(
                monitor,
                "collect_configuration",
                side_effect=AssertionError("configuration query must not start"),
            ):
                owner = self._owner(
                    "diagnostic",
                    "simulate",
                    target=target,
                    snapshot_id=inventory.snapshot_id,
                )
                app._configuration_worker(
                    owner,
                    "simulate",
                    "nominal",
                    output_root,
                    target,
                    inventory.snapshot_id,
                    inventory,
                    source_file,
                )

            queued_owner, event, payload = app.events.get_nowait()
            self.assertIs(queued_owner, owner)
            self.assertEqual(event, "configuration_error")
            message, _run_directory, _failure_file, _journal, context = payload
            self.assertIn("does not match frozen snapshot", message)
            self.assertIsNone(context)

    def test_target_outside_snapshot_blocks_before_configuration_query(self):
        app = self._worker_app()
        full_inventory = monitor.build_simulated_inventory()
        target = monitor.target_from_inventory_entry(full_inventory.entries[2])
        self.assertIsNotNone(target)
        assert target is not None
        narrow_inventory = monitor.build_simulated_inventory(
            (
                SimulatedInstrument(
                    "GPIB0::6::INSTR",
                    "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,1340129,C02  /A02",
                ),
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            source_file = output_root / "inventory-snapshot.json"
            monitor.write_json_atomic(source_file, narrow_inventory.as_dict())
            with mock.patch.object(
                monitor,
                "collect_configuration",
                side_effect=AssertionError("configuration query must not start"),
            ):
                owner = self._owner(
                    "diagnostic",
                    "simulate",
                    target=target,
                    snapshot_id=narrow_inventory.snapshot_id,
                )
                app._configuration_worker(
                    owner,
                    "simulate",
                    "nominal",
                    output_root,
                    target,
                    narrow_inventory.snapshot_id,
                    narrow_inventory,
                    source_file,
                )

            queued_owner, event, payload = app.events.get_nowait()
            self.assertIs(queued_owner, owner)
            self.assertEqual(event, "configuration_error")
            message, _run_directory, _failure_file, _journal, context = payload
            self.assertIn("not present in the frozen inventory", message)
            self.assertIsNone(context)


if __name__ == "__main__":
    unittest.main()
