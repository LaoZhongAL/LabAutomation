from __future__ import annotations

import unittest

import instrument_profiles as profiles


def row_values(target_key: str, values: dict[str, object]) -> dict[str, str]:
    return {
        row.key: row.value
        for row in profiles.summary_rows_for_target(target_key, values)
    }


class KnownAssetFixtureTests(unittest.TestCase):
    def test_simulation_fixtures_match_the_five_handoff_assets(self):
        self.assertEqual(tuple(profiles.TARGETS), profiles.TARGET_ORDER)
        self.assertEqual(
            {
                key: (
                    target.resource,
                    target.model,
                    target.serial,
                    target.firmware,
                )
                for key, target in profiles.TARGETS.items()
            },
            {
                "2182a-gpib6": ("GPIB0::6::INSTR", "2182A", "1340129", "C02 /A02"),
                "2182a-gpib7": ("GPIB0::7::INSTR", "2182A", "4510267", "C08/B01"),
                "6221-gpib9": ("GPIB0::9::INSTR", "6221", "4533811", "D04 /700x"),
                "6221-gpib10": ("GPIB0::10::INSTR", "6221", "4581062", "D04 /700x"),
                "2450-gpib25": ("GPIB0::25::INSTR", "2450", "04584128", "1.7.12b"),
            },
        )
        self.assertNotIn("GPIB0::26::INSTR", {item.resource for item in profiles.TARGETS.values()})

    def test_registry_has_three_strict_model_profiles(self):
        self.assertEqual(set(profiles.PROFILES), {"2182a", "6221", "2450"})
        self.assertEqual(profiles.PROFILES["2182a"].command_set, profiles.CommandSet.SCPI)
        self.assertEqual(profiles.PROFILES["6221"].command_set, profiles.CommandSet.SCPI)
        self.assertEqual(profiles.PROFILES["2450"].command_set, profiles.CommandSet.TSP)
        for target in profiles.TARGETS.values():
            self.assertEqual(target.model, profiles.PROFILES[target.profile_key].model)

    def test_exact_model_resolution_reuses_profiles_and_2450_needs_tsp_ack(self):
        self.assertIs(
            profiles.profile_for_model("2182A"),
            profiles.PROFILES["2182a"],
        )
        self.assertIs(
            profiles.profile_for_model("6221"),
            profiles.PROFILES["6221"],
        )
        self.assertIsNone(profiles.profile_for_model("MODEL 2182A"))
        self.assertIsNone(profiles.profile_for_model("2450"))
        self.assertIs(
            profiles.profile_for_model("2450", command_set_ack="TSP"),
            profiles.PROFILES["2450"],
        )

    def test_future_same_model_target_uses_profile_without_live_approval(self):
        target = profiles.InstrumentTarget(
            key="runtime-gpib12",
            label="Keithley 2182A · GPIB12 · S/N FUTURE001",
            resource="GPIB0::12::INSTR",
            vendor="KEITHLEY INSTRUMENTS INC.",
            model="2182A",
            serial="FUTURE001",
            firmware="C11/A02",
            profile_key="2182a",
            live_supported=False,
        )
        rows = profiles.summary_rows_for_profile("2182a", target, {})
        self.assertTrue(rows)
        self.assertEqual(
            profiles.validate_profile_read_transaction(
                "2182a", "*IDN?", phase="diagnostic"
            ),
            "*IDN?",
        )
        with self.assertRaises(profiles.UnsafeReadTransaction):
            profiles.validate_profile_read_transaction(
                "2182a", "FETCh?", phase="live", live_approved=False
            )

    def test_unknown_or_wrong_case_target_is_not_inferred(self):
        for key in ("2182A-GPIB6", "gpib6", "GPIB0::6::INSTR", "2450-gpib26"):
            with self.subTest(key=key), self.assertRaises(KeyError):
                profiles.target_for(key)


class QueryPolicyTests(unittest.TestCase):
    def test_2182a_model_uses_28_diagnostics_plus_separate_fetch_live(self):
        commands = profiles.allowed_transactions_for_target("2182a-gpib6")
        self.assertEqual(len(commands), 28)
        self.assertEqual(commands, tuple(spec.command for spec in profiles.Q2182A))
        for command in (
            "SYST:AZERO?",
            "SYST:FAZERO?",
            "SYST:LSYNC?",
            "STAT:OPER:COND?",
            "STAT:MEAS:COND?",
            "STAT:QUES:COND?",
        ):
            self.assertIn(command, commands)
        self.assertNotIn("FETCh?", commands)
        self.assertEqual(
            profiles.allowed_transactions_for_target("2182a-gpib6", phase="live"),
            ("*IDN?", "FETCh?"),
        )
        self.assertEqual(
            profiles.validate_read_transaction(
                "2182a-gpib6",
                "FETCh?",
                phase="live",
            ),
            "FETCh?",
        )

    def test_gpib7_is_diagnostic_only_even_with_same_2182a_profile(self):
        self.assertEqual(
            profiles.allowed_transactions_for_target("2182a-gpib7"),
            profiles.allowed_transactions_for_target("2182a-gpib6"),
        )
        self.assertEqual(
            profiles.allowed_transactions_for_target("2182a-gpib7", phase="live"),
            (),
        )
        self.assertIsNone(profiles.live_query_for_target("2182a-gpib7"))
        with self.assertRaises(profiles.UnsafeReadTransaction):
            profiles.validate_read_transaction("2182a-gpib7", "FETCh?", phase="live")

    def test_6221_and_2450_have_no_live_transactions(self):
        for key in ("6221-gpib9", "6221-gpib10", "2450-gpib25"):
            with self.subTest(target=key):
                self.assertEqual(
                    profiles.allowed_transactions_for_target(key, phase="live"),
                    (),
                )
                self.assertIsNone(profiles.live_query_for_target(key))

    def test_every_declared_diagnostic_request_passes_its_exact_target_policy(self):
        for target_key in profiles.TARGET_ORDER:
            commands = profiles.allowed_transactions_for_target(target_key)
            self.assertGreater(len(commands), 0)
            for command in commands:
                with self.subTest(target=target_key, command=command):
                    self.assertEqual(
                        profiles.validate_read_transaction(target_key, command),
                        command,
                    )

    def test_scpi_profiles_contain_only_single_question_queries(self):
        for profile_key in ("2182a", "6221"):
            for spec in profiles.PROFILES[profile_key].diagnostic_queries:
                with self.subTest(profile=profile_key, command=spec.command):
                    self.assertEqual(spec.command.count("?"), 1)
                    self.assertTrue(spec.command.endswith("?"))
                    self.assertFalse(any(char in spec.command for char in ";\r\n"))

    def test_exact_matching_rejects_prefix_case_compound_and_write_variants(self):
        attempts = (
            ("6221-gpib9", "OUTP? trailing"),
            ("6221-gpib9", "outp?"),
            ("6221-gpib9", "OUTP?;*RST"),
            ("6221-gpib9", "OUTP?\n*IDN?"),
            ("6221-gpib9", "*RST"),
            ("2182a-gpib6", "FETCh?"),
            ("2182a-gpib6", "READ?"),
            ("2182a-gpib6", "MEAS?"),
        )
        for target, command in attempts:
            with self.subTest(target=target, command=command), self.assertRaises(
                profiles.UnsafeReadTransaction
            ):
                profiles.validate_read_transaction(target, command)

    def test_6221_uses_only_the_conservative_approved_set(self):
        commands = set(profiles.PROFILES["6221"].diagnostic_commands)
        self.assertEqual(
            commands,
            {
                "*IDN?",
                "OUTP?",
                "OUTP:INTERLOCK:TRIPPED?",
                "STAT:OPER:COND?",
                "STAT:MEAS:COND?",
                "STAT:QUES:COND?",
                "*STB?",
                "SOUR:CURR:RANG?",
                "SOUR:CURR:RANG:AUTO?",
                "SOUR:CURR:COMP?",
                "SOUR:CURR:FILT?",
                "OUTP:RESPONSE?",
                "OUTP:ISHIELD?",
                "OUTP:LTEARTH?",
            },
        )
        for forbidden in (
            "SOUR:CURR?",
            "*ESR?",
            "SYST:ERR?",
            "STAT:OPER:EVEN?",
            "STAT:MEAS:EVEN?",
            "STAT:QUES:EVEN?",
            "READ?",
            "MEAS?",
        ):
            self.assertNotIn(forbidden, commands)

    def test_2450_is_limited_to_idn_and_simple_exact_print_attributes(self):
        commands = profiles.PROFILES["2450"].diagnostic_commands
        self.assertIn("*IDN?", commands)
        self.assertIn("print(localnode.model)", commands)
        self.assertIn("print(smu.measure.autozero.enable)", commands)
        self.assertIn("print(smu.measure.filter.count)", commands)
        self.assertIn("print(smu.source.ilimit.level)", commands)
        self.assertIn("print(smu.source.ilimit.tripped)", commands)
        self.assertIn("print(smu.source.vlimit.level)", commands)
        self.assertIn("print(smu.source.vlimit.tripped)", commands)
        self.assertIn("print(smu.source.protect.tripped)", commands)
        self.assertIn("print(smu.interlock.tripped)", commands)
        self.assertIn("print(status.condition)", commands)
        self.assertIn("print(status.operation.condition)", commands)
        self.assertIn("print(status.questionable.condition)", commands)
        for command in commands:
            with self.subTest(command=command):
                self.assertNotIn("=", command)
                self.assertFalse(any(char in command for char in ";\r\n"))
                if command != "*IDN?":
                    self.assertTrue(command.startswith("print("))
                    self.assertTrue(command.endswith(")"))
                    self.assertEqual(command.count("("), 1)
                    self.assertEqual(command.count(")"), 1)

    def test_2450_rejects_assignment_calls_measure_read_and_event_status_access(self):
        attempts = (
            "smu.source.output=1",
            "print(smu.source.output);smu.source.output=1",
            "print(smu.measure.read())",
            "print(smu.measure.read)",
            "print(status.operation.event)",
            "print(status.operation.enable)",
            "print(status.standard.event)",
            "print(trigger.model.state)",
            "print(trigger.model.state())",
            "print(event.status)",
            "print(lan.ipconfig())",
            "print(smu.source)",
            "print(smu.source.output.extra)",
            "print(SMU.source.output)",
        )
        for command in attempts:
            with self.subTest(command=command), self.assertRaises(
                profiles.UnsafeReadTransaction
            ):
                profiles.validate_read_transaction("2450-gpib25", command)

    def test_snapshot_end_and_consistency_fields_are_profile_specific(self):
        expected = {
            "2182a": (
                (
                    "identity",
                    "sense_function",
                    "active_channel",
                    "nplc",
                    "system_autozero",
                    "front_autozero",
                    "line_sync",
                    "operation_condition",
                    "measurement_condition",
                    "questionable_condition",
                ),
                (
                    "identity",
                    "sense_function",
                    "active_channel",
                    "nplc",
                    "system_autozero",
                    "front_autozero",
                    "line_sync",
                ),
            ),
            "6221": (
                (
                    "identity",
                    "output_enabled",
                    "operation_condition",
                    "current_range_a",
                    "current_range_auto",
                    "voltage_compliance_v",
                    "analog_filter",
                ),
                (
                    "identity",
                    "output_enabled",
                    "current_range_a",
                    "current_range_auto",
                    "voltage_compliance_v",
                    "analog_filter",
                ),
            ),
            "2450": (
                (
                    "identity",
                    "source_output",
                    "source_function",
                    "source_level",
                    "measure_function",
                    "terminals",
                    "measure_nplc",
                    "status_condition",
                    "operation_condition",
                    "questionable_condition",
                ),
                (
                    "identity",
                    "source_output",
                    "source_function",
                    "source_level",
                    "measure_function",
                    "terminals",
                    "measure_nplc",
                ),
            ),
        }
        for profile_key, (end_names, consistency_names) in expected.items():
            with self.subTest(profile=profile_key):
                profile = profiles.PROFILES[profile_key]
                self.assertEqual(profile.snapshot_end_names, end_names)
                self.assertEqual(profile.consistency_names, consistency_names)

    def test_a_command_allowed_for_one_profile_is_not_reused_for_another(self):
        attempts = (
            ("6221-gpib9", "SENS:VOLT:DC:NPLC?"),
            ("2182a-gpib6", "OUTP?"),
            ("2450-gpib25", "OUTP?"),
            ("2182a-gpib7", "print(localnode.model)"),
        )
        for target, command in attempts:
            with self.subTest(target=target, command=command), self.assertRaises(
                profiles.UnsafeReadTransaction
            ):
                profiles.validate_read_transaction(target, command)

    def test_2450_compliance_queries_are_conditioned_on_source_function(self):
        by_name = {spec.name: spec for spec in profiles.Q2450}
        voltage_values = {"source_function": "1"}
        current_values = {"source_function": "0"}

        self.assertTrue(
            profiles.query_is_applicable(by_name["source_current_limit_a"], voltage_values)
        )
        self.assertTrue(
            profiles.query_is_applicable(by_name["current_limit_tripped"], voltage_values)
        )
        self.assertFalse(
            profiles.query_is_applicable(by_name["source_voltage_limit_v"], voltage_values)
        )
        self.assertTrue(
            profiles.query_is_applicable(by_name["source_voltage_limit_v"], current_values)
        )
        self.assertFalse(
            profiles.query_is_applicable(by_name["source_current_limit_a"], current_values)
        )

    def test_query_names_and_commands_are_unique_inside_each_profile(self):
        for profile in profiles.PROFILES.values():
            names = [spec.name for spec in profile.diagnostic_queries]
            commands = [spec.command for spec in profile.diagnostic_queries]
            self.assertEqual(len(names), len(set(names)), profile.key)
            self.assertEqual(len(commands), len(set(commands)), profile.key)


class AdaptiveSummaryTests(unittest.TestCase):
    REQUIRED_KEYS = {
        "accuracy_noise",
        "range",
        "integration",
        "filter",
        "remote_sense",
        "compliance",
    }

    def test_every_model_has_the_required_precision_summary_rows(self):
        for target in ("2182a-gpib6", "6221-gpib9", "2450-gpib25"):
            with self.subTest(target=target):
                rows = row_values(target, {})
                self.assertTrue(self.REQUIRED_KEYS.issubset(rows))
                self.assertEqual(rows["accuracy_noise"], "not characterized")

    def test_2182a_summary_follows_active_channel_and_calculates_integration(self):
        rows = row_values(
            "2182a-gpib6",
            {
                "identity": "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,1340129,C02  /A02",
                "sense_function": '"VOLT:DC"',
                "active_channel": "1",
                "nplc": "5.00",
                "line_frequency_hz": "50",
                "ch1_range_v": "0.010000",
                "ch1_autorange": "0",
                "ch1_digital_filter": "0",
                "ch1_analog_filter": "0",
                "trigger_source": "IMM",
                "trigger_count": "+9.9e37",
                "trigger_delay_s": "0.000",
                "continuous_initiation": "1",
                "system_autozero": "1",
                "front_autozero": "0",
                "line_sync": "1",
                "operation_condition": "16",
                "measurement_condition": "32",
                "questionable_condition": "0",
            },
        )
        self.assertIn("CH1: 0.010000 V", rows["range"])
        self.assertIn("autorange OFF", rows["range"])
        self.assertIn("~0.1 s", rows["integration"])
        self.assertIn("digital OFF", rows["filter"])
        self.assertEqual(rows["remote_sense"], "N/A")
        self.assertEqual(rows["compliance"], "N/A")
        self.assertEqual(rows["zero_sync"], "system ON; front OFF; LSYNC ON")
        self.assertEqual(rows["status_conditions"], "OPER 16; MEAS 32; QUES 0")

    def test_6221_summary_keeps_guard_separate_from_remote_sense(self):
        rows = row_values(
            "6221-gpib9",
            {
                "output_enabled": "0",
                "interlock_tripped_raw": "1",
                "current_range_a": "0.100",
                "current_range_auto": "0",
                "voltage_compliance_v": "10",
                "measurement_condition": "0",
                "analog_filter": "1",
                "output_response": "FAST",
                "triax_inner_shield": "GUARD",
                "output_low_to_earth": "0",
            },
        )
        self.assertIn("0.100 A", rows["range"])
        self.assertEqual(rows["integration"], "N/A")
        self.assertEqual(rows["remote_sense"], "N/A")
        self.assertEqual(rows["compliance"], "limit 10 V; active N/A (output OFF)")
        self.assertEqual(rows["filter"], "ON")
        self.assertIn("inner shield GUARD", rows["output_path"])

    def test_2450_voltage_source_summary_selects_current_limit(self):
        rows = row_values(
            "2450-gpib25",
            {
                "source_output": "0",
                "source_off_mode": "0",
                "source_function": "1",
                "source_level": "0.25",
                "source_autorange": "0",
                "source_range": "2",
                "source_readback": "1",
                "measure_function": "0",
                "measure_sense": "1",
                "measure_autorange": "0",
                "measure_range": "0.001",
                "measure_nplc": "5",
                "line_frequency_hz": "50",
                "measure_filter_enable": "1",
                "measure_filter_type": "0",
                "measure_filter_count": "10",
                "interlock_enabled": "1",
                "interlock_asserted": "1",
                "source_current_limit_a": "0.001",
                "current_limit_tripped": "0",
            },
        )
        self.assertEqual(rows["compliance"], "current limit 0.001 A; reached OFF")
        self.assertIn("2-wire effective while output OFF", rows["remote_sense"])
        self.assertIn("configured 1", rows["remote_sense"])
        self.assertIn("~0.1 s", rows["integration"])
        self.assertIn("count 10", rows["filter"])
        self.assertIn("interlock asserted ON", rows["output"])

    def test_2450_summary_preserves_raw_condition_words(self):
        rows = row_values(
            "2450-gpib25",
            {
                "status_condition": "1.29000e+02",
                "operation_condition": "3",
                "questionable_condition": "4",
            },
        )
        self.assertEqual(
            rows["status_conditions"],
            "STB 1.29000e+02; OPER 3; QUES 4",
        )

    def test_2450_current_source_summary_selects_voltage_limit(self):
        rows = row_values(
            "2450-gpib25",
            {
                "source_output": "1",
                "source_function": "0",
                "measure_sense": "1",
                "source_voltage_limit_v": "10",
                "voltage_limit_tripped": "0",
            },
        )
        self.assertEqual(rows["compliance"], "voltage limit 10 V; reached OFF")
        self.assertEqual(rows["remote_sense"], "configured 1")


if __name__ == "__main__":
    unittest.main()
