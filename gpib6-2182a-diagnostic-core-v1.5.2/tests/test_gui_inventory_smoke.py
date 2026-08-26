from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path

import gpib6_2182a_monitor as monitor


class GuiInventorySmokeTests(unittest.TestCase):
    def setUp(self):
        if os.environ.get("RUN_TK_SMOKE") != "1":
            self.skipTest("set RUN_TK_SMOKE=1 for the local hidden-Tk smoke test")
        try:
            import tkinter as tk

            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as exc:
            self.skipTest(f"Tk is unavailable in this test environment: {exc}")
        self.app = monitor.MonitorApp(self.root)

    def tearDown(self):
        root = getattr(self, "root", None)
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass

    def test_explicit_simulated_refresh_populates_runtime_inventory(self):
        self.assertEqual(tuple(self.app.target_combo.cget("values")), ())
        self.assertIsNone(self.app.inventory_snapshot)
        self.assertEqual(str(self.app.config_button.cget("state")), "disabled")

        with tempfile.TemporaryDirectory() as temp_dir:
            owner = self.app._begin_operation(kind="inventory", mode="simulate")
            self.app._inventory_worker(owner, "simulate", Path(temp_dir))
            self.app._drain_events()

        values = tuple(self.app.target_combo.cget("values"))
        self.assertEqual(len(values), 5)
        self.assertEqual(self.app.inventory_snapshot.source, "simulate")
        self.assertEqual(self.app._current_target().resource, "GPIB0::6::INSTR")
        self.assertTrue(self.app._current_target().live_supported)

        label_6221 = next(
            label
            for label, entry in self.app.inventory_label_to_entry.items()
            if entry.resource == "GPIB0::9::INSTR"
        )
        self.app.target_var.set(label_6221)
        self.app._target_changed()
        selected = self.app._current_target()
        self.assertEqual(selected.profile_key, "6221")
        self.assertFalse(selected.live_supported)
        self.assertEqual(str(self.app.config_button.cget("state")), "normal")
        self.assertEqual(str(self.app.start_button.cget("state")), "disabled")

    def test_real_mode_starts_with_no_inventory_or_visa_access(self):
        self.app.mode_var.set("real")
        self.app._mode_changed()

        self.assertEqual(self.app.selected_mode, "real")
        self.assertIsNone(self.app.inventory_snapshot)
        self.assertEqual(tuple(self.app.target_combo.cget("values")), ())
        self.assertIsNone(self.app._current_target())
        self.assertFalse(self.app.real_access_confirmed)
        self.assertEqual(str(self.app.config_button.cget("state")), "disabled")

    def test_health_axes_card_uses_the_configuration_report(self):
        self.assertEqual(self.app.health_axes_var.get(), "Health axes: not evaluated")

        with tempfile.TemporaryDirectory() as temp_dir:
            owner = self.app._begin_operation(
                kind="diagnostic",
                mode="simulate",
                target_key=monitor.DEFAULT_TARGET_KEY,
            )
            self.app._configuration_worker(
                owner,
                "simulate",
                "nominal",
                Path(temp_dir),
            )
            self.app._drain_events()

        health_text = self.app.health_axes_var.get()
        self.assertTrue(health_text.startswith("Health axes: identity verified PASS"))
        self.assertIn("performance validated UNKNOWN", health_text)
        self.assertIn("live authorized PASS", health_text)
        self.app._finalize_diagnostic_run("test_complete")

    def test_switching_back_shows_retained_result_as_stale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            owner = self.app._begin_operation(kind="inventory", mode="simulate")
            self.app._inventory_worker(owner, "simulate", Path(temp_dir))
            self.app._drain_events()
            target = self.app._current_target()
            snapshot = self.app.inventory_snapshot
            owner = self.app._begin_operation(
                kind="diagnostic",
                mode="simulate",
                target_key=target.key,
                inventory_snapshot_id=snapshot.snapshot_id,
            )
            self.app._configuration_worker(
                owner,
                "simulate",
                "nominal",
                Path(temp_dir),
            )
            self.app._drain_events()

        label_gpib6 = next(
            label
            for label, entry in self.app.inventory_label_to_entry.items()
            if entry.resource == "GPIB0::6::INSTR"
        )
        label_6221 = next(
            label
            for label, entry in self.app.inventory_label_to_entry.items()
            if entry.resource == "GPIB0::9::INSTR"
        )
        self.app.target_var.set(label_6221)
        self.app._target_changed()
        self.app.target_var.set(label_gpib6)
        self.app._target_changed()

        self.assertIsNone(self.app.configuration)
        self.assertIsNone(self.app.diagnostic_target)
        self.assertIn("STALE", self.app.readiness_var.get())
        self.assertEqual(str(self.app.start_button.cget("state")), "disabled")


if __name__ == "__main__":
    unittest.main()
