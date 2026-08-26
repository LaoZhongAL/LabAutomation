import tempfile
import unittest
from pathlib import Path

from gpib_terminal import (
    ACTIVE_QUERY_CONFIRMATION,
    HIGH_RISK_WRITE_CONFIRMATION,
    LAB_INSTRUMENTS,
    WRITE_UNLOCK_PHRASE,
    GpibTerminal,
    JsonlSessionLog,
    is_active_query,
    is_high_risk_write,
    parse_device_operation,
)


class FakeBackend(object):
    def __init__(self):
        self.calls = []

    def list_resources(self):
        self.calls.append(("list",))
        return tuple(LAB_INSTRUMENTS)

    def query(self, resource, message, timeout_ms):
        self.calls.append(("query", resource, message, timeout_ms))
        return "KEITHLEY,MODEL 6221,TEST,D04\n"

    def write(self, resource, message, timeout_ms):
        self.calls.append(("write", resource, message, timeout_ms))
        return len(message)


class ScriptedInput(object):
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self, prompt):
        return next(self.values)


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.backend = FakeBackend()
        self.output = []

    def tearDown(self):
        self.temp_directory.cleanup()

    def terminal(self, confirmations=()):
        return GpibTerminal(
            backend=self.backend,
            session_log=JsonlSessionLog(Path(self.temp_directory.name)),
            input_fn=ScriptedInput(confirmations),
            output_fn=self.output.append,
        )

    def test_parser_preserves_real_message(self):
        resource, message = parse_device_operation(
            'GPIB0::25::INSTR print(smu.measure.func)'
        )
        self.assertEqual(resource, "GPIB0::25::INSTR")
        self.assertEqual(message, "print(smu.measure.func)")

    def test_safe_query_sends_exactly_one_message_without_unlock(self):
        terminal = self.terminal()
        terminal.execute_line("QUERY GPIB0::9::INSTR *IDN?")
        self.assertEqual(
            self.backend.calls,
            [("query", "GPIB0::9::INSTR", "*IDN?", 3000)],
        )
        self.assertTrue(any(text.startswith("TX -> *IDN?") for text in self.output))
        self.assertTrue(any(text.startswith("RX <- KEITHLEY") for text in self.output))

    def test_write_is_blocked_by_default(self):
        terminal = self.terminal()
        terminal.execute_line("WRITE GPIB0::9::INSTR OUTP OFF")
        self.assertEqual(self.backend.calls, [])
        self.assertTrue(any("WRITE is locked" in text for text in self.output))

    def test_high_risk_write_needs_unlock_and_exact_second_confirmation(self):
        terminal = self.terminal([HIGH_RISK_WRITE_CONFIRMATION])
        terminal.execute_line("UNLOCK-WRITES " + WRITE_UNLOCK_PHRASE)
        terminal.execute_line("WRITE GPIB0::9::INSTR SOUR:CURR 1E-6")
        self.assertEqual(
            self.backend.calls,
            [("write", "GPIB0::9::INSTR", "SOUR:CURR 1E-6", 3000)],
        )

    def test_active_measurement_query_needs_confirmation(self):
        terminal = self.terminal([ACTIVE_QUERY_CONFIRMATION])
        terminal.execute_line("QUERY GPIB0::6::INSTR SENS:DATA:FRESH?")
        self.assertEqual(
            self.backend.calls[0],
            ("query", "GPIB0::6::INSTR", "SENS:DATA:FRESH?", 3000),
        )

    def test_bad_active_query_confirmation_cancels_io(self):
        terminal = self.terminal(["SEND"])
        terminal.execute_line("QUERY GPIB0::6::INSTR SENS:DATA:FRESH?")
        self.assertEqual(self.backend.calls, [])

    def test_semicolon_multi_message_is_blocked(self):
        terminal = self.terminal()
        terminal.execute_line("QUERY GPIB0::9::INSTR *IDN?;OUTP ON")
        self.assertEqual(self.backend.calls, [])

    def test_unknown_resource_is_blocked(self):
        terminal = self.terminal()
        terminal.execute_line("QUERY GPIB0::99::INSTR *IDN?")
        self.assertEqual(self.backend.calls, [])

    def test_list_only_enumerates(self):
        terminal = self.terminal()
        terminal.execute_line("LIST")
        self.assertEqual(self.backend.calls, [("list",)])

    def test_local_resistance_calculation_does_not_call_visa(self):
        terminal = self.terminal()
        terminal.execute_line("CALC-R 1.25E-3 2.5E-6")
        self.assertEqual(self.backend.calls, [])
        self.assertTrue(any("500 ohm" in text for text in self.output))

    def test_risk_classification(self):
        self.assertTrue(is_active_query("SENS:DATA:FRESH?"))
        self.assertFalse(is_active_query("SENS:DATA:LATEST?"))
        self.assertTrue(is_high_risk_write("OUTP ON"))
        self.assertTrue(is_high_risk_write("SOUR:CURR 1E-6"))
        self.assertFalse(is_high_risk_write("OUTP OFF"))


if __name__ == "__main__":
    unittest.main()
