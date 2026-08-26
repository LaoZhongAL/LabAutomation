from __future__ import annotations

import dataclasses
import unittest
from collections import Counter

import instrument_inventory as inventory


IDN_2182A_6 = "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,1340129,C02  /A02"
IDN_2182A_NEW = "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,NEW2182,C11/A03"
IDN_6221_9 = "KEITHLEY INSTRUMENTS INC.,MODEL 6221,4533811,D04  /700x"
IDN_2450_25 = "KEITHLEY INSTRUMENTS,MODEL 2450,04584128,1.7.12b"
IDN_2450_NEW = "KEITHLEY INSTRUMENTS,MODEL 2450,NEW2450,2.0.0"


class FakeResource:
    def __init__(self, manager, name, result, *, close_error=None):
        self.manager = manager
        self.name = name
        self.result = result
        self.close_error = close_error
        self.timeout = None
        self.closed = False

    def query(self, command):
        self.manager.query_log.append((self.name, command))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.manager.active_count -= 1
        self.manager.resource_close_log.append(self.name)
        if self.close_error is not None:
            raise self.close_error


class FakeResourceManager:
    def __init__(
        self,
        raw_resources,
        responses,
        *,
        open_errors=None,
        close_errors=None,
        list_error=None,
        manager_close_error=None,
    ):
        self.raw_resources = tuple(raw_resources)
        self.responses = dict(responses)
        self.open_errors = dict(open_errors or {})
        self.close_errors = dict(close_errors or {})
        self.list_error = list_error
        self.manager_close_error = manager_close_error
        self.list_calls = 0
        self.open_order = []
        self.query_log = []
        self.resources = []
        self.resource_close_log = []
        self.active_count = 0
        self.max_active_count = 0
        self.closed = False

    def list_resources(self):
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return self.raw_resources

    def open_resource(self, resource_name):
        self.open_order.append(resource_name)
        if resource_name in self.open_errors:
            raise self.open_errors[resource_name]
        resource = FakeResource(
            self,
            resource_name,
            self.responses[resource_name],
            close_error=self.close_errors.get(resource_name),
        )
        self.resources.append(resource)
        self.active_count += 1
        self.max_active_count = max(self.max_active_count, self.active_count)
        return resource

    def close(self):
        self.closed = True
        if self.manager_close_error is not None:
            raise self.manager_close_error


def run_refresh(manager, **kwargs):
    factory_calls = []

    def factory():
        factory_calls.append(True)
        return manager

    snapshot = inventory.refresh_inventory(
        resource_manager_factory=factory,
        **kwargs,
    )
    return snapshot, factory_calls


class ResourceFilteringTests(unittest.TestCase):
    def test_filter_accepts_only_gpib0_primary_1_to_30_instr(self):
        raw = (
            "TCPIP0::192.0.2.1::INSTR",
            "GPIB1::6::INSTR",
            "GPIB0::0::INSTR",
            "GPIB0::31::INSTR",
            "GPIB0::6::INTFC",
            "GPIB0::8::0::INSTR",
            "GPIB0::06::INSTR",
            "GPIB0::30::INSTR",
            "GPIB0::1::INSTR",
            "gpib0::9::instr",
            "GPIB0::6::INSTR",
            "gpib0::6::instr",
        )
        self.assertEqual(
            inventory.filter_gpib0_resources(raw),
            (
                "GPIB0::1::INSTR",
                "GPIB0::6::INSTR",
                "gpib0::9::instr",
                "GPIB0::30::INSTR",
            ),
        )

    def test_refresh_opens_only_resources_returned_and_retained_by_filter(self):
        raw = (
            "USB0::1::INSTR",
            "GPIB0::7::INSTR",
            "GPIB0::6::INSTR",
            "GPIB1::9::INSTR",
            "GPIB0::10::INTFC",
        )
        manager = FakeResourceManager(
            raw,
            {
                "GPIB0::6::INSTR": IDN_2182A_6,
                "GPIB0::7::INSTR": IDN_2182A_NEW,
            },
        )
        snapshot, _factory_calls = run_refresh(manager)
        self.assertEqual(manager.open_order, ["GPIB0::6::INSTR", "GPIB0::7::INSTR"])
        self.assertEqual(snapshot.raw_resources, raw)
        self.assertEqual(snapshot.filtered_resources, tuple(manager.open_order))


class ExplicitSequentialRefreshTests(unittest.TestCase):
    def setUp(self):
        self.raw = (
            "GPIB0::25::INSTR",
            "GPIB0::6::INSTR",
            "GPIB0::9::INSTR",
        )
        self.responses = {
            "GPIB0::6::INSTR": IDN_2182A_6,
            "GPIB0::9::INSTR": IDN_6221_9,
            "GPIB0::25::INSTR": IDN_2450_25,
        }

    def test_list_once_one_idn_each_timeout_2000_and_maximum_one_open(self):
        manager = FakeResourceManager(self.raw, self.responses)
        snapshot, factory_calls = run_refresh(manager)

        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(manager.list_calls, 1)
        self.assertEqual(manager.max_active_count, 1)
        self.assertEqual(manager.active_count, 0)
        self.assertTrue(manager.closed)
        self.assertEqual(
            manager.open_order,
            ["GPIB0::6::INSTR", "GPIB0::9::INSTR", "GPIB0::25::INSTR"],
        )
        self.assertEqual(
            manager.query_log,
            [(name, inventory.IDENTITY_QUERY) for name in manager.open_order],
        )
        self.assertTrue(
            all(resource.timeout == inventory.INVENTORY_TIMEOUT_MS for resource in manager.resources)
        )
        self.assertTrue(all(resource.closed for resource in manager.resources))
        self.assertEqual(snapshot.counts.filtered_gpib_count, 3)
        self.assertEqual(snapshot.counts.identity_response_count, 3)

    def test_query_error_is_not_retried_and_later_resources_continue(self):
        manager = FakeResourceManager(
            ("GPIB0::6::INSTR", "GPIB0::7::INSTR", "GPIB0::9::INSTR"),
            {
                "GPIB0::6::INSTR": IDN_2182A_6,
                "GPIB0::7::INSTR": TimeoutError("identity timeout"),
                "GPIB0::9::INSTR": IDN_6221_9,
            },
        )
        snapshot, _factory_calls = run_refresh(manager)
        counts = Counter(name for name, command in manager.query_log)
        self.assertEqual(counts, Counter({name: 1 for name in manager.open_order}))
        self.assertEqual(manager.open_order[-1], "GPIB0::9::INSTR")
        self.assertTrue(all(resource.closed for resource in manager.resources))
        self.assertEqual(snapshot.entries[1].status, "io_error")
        self.assertIn("TimeoutError", snapshot.entries[1].error)
        self.assertEqual(snapshot.entries[2].profile_key, "6221")
        self.assertEqual(snapshot.counts.io_error_count, 1)

    def test_open_error_continues_without_sending_an_identity_query(self):
        manager = FakeResourceManager(
            ("GPIB0::6::INSTR", "GPIB0::9::INSTR"),
            {"GPIB0::9::INSTR": IDN_6221_9},
            open_errors={"GPIB0::6::INSTR": OSError("open failed")},
        )
        snapshot, _factory_calls = run_refresh(manager)
        self.assertEqual(manager.open_order, ["GPIB0::6::INSTR", "GPIB0::9::INSTR"])
        self.assertEqual(manager.query_log, [("GPIB0::9::INSTR", "*IDN?")])
        self.assertEqual(snapshot.entries[0].status, "io_error")
        self.assertEqual(snapshot.entries[1].profile_key, "6221")

    def test_resource_close_error_is_recorded_and_next_resource_still_runs(self):
        manager = FakeResourceManager(
            ("GPIB0::6::INSTR", "GPIB0::9::INSTR"),
            {
                "GPIB0::6::INSTR": IDN_2182A_6,
                "GPIB0::9::INSTR": IDN_6221_9,
            },
            close_errors={"GPIB0::6::INSTR": OSError("close failed")},
        )
        snapshot, _factory_calls = run_refresh(manager)
        self.assertEqual(manager.max_active_count, 1)
        self.assertEqual(manager.open_order, ["GPIB0::6::INSTR", "GPIB0::9::INSTR"])
        self.assertEqual(snapshot.entries[0].status, "resource_close_error")
        self.assertIn("close failed", snapshot.entries[0].error)
        self.assertEqual(snapshot.entries[1].profile_key, "6221")

    def test_list_failure_still_closes_manager_and_does_not_retry(self):
        manager = FakeResourceManager((), {}, list_error=OSError("enumeration failed"))
        snapshot, _factory_calls = run_refresh(manager)
        self.assertEqual(manager.list_calls, 1)
        self.assertTrue(manager.closed)
        self.assertEqual(manager.open_order, [])
        self.assertIn("enumeration failed", snapshot.refresh_error)

    def test_manager_close_failure_is_preserved_in_snapshot(self):
        manager = FakeResourceManager(
            ("GPIB0::6::INSTR",),
            {"GPIB0::6::INSTR": IDN_2182A_6},
            manager_close_error=OSError("manager close failed"),
        )
        snapshot, _factory_calls = run_refresh(manager)
        self.assertTrue(manager.closed)
        self.assertIn("manager close failed", snapshot.manager_close_error)


class ProfileResolutionTests(unittest.TestCase):
    def test_unknown_model_and_malformed_identity_never_receive_a_profile(self):
        manager = FakeResourceManager(
            ("GPIB0::11::INSTR", "GPIB0::12::INSTR"),
            {
                "GPIB0::11::INSTR": "KEITHLEY INSTRUMENTS INC.,MODEL 9999,U1,1.0",
                "GPIB0::12::INSTR": "not,a,valid,idn,response",
            },
        )
        snapshot, _factory_calls = run_refresh(manager)
        unknown, malformed = snapshot.entries
        self.assertEqual(unknown.status, "unknown_model")
        self.assertIsNone(unknown.profile_key)
        self.assertEqual(malformed.status, "malformed_identity")
        self.assertIsNone(malformed.identity)
        self.assertIsNone(malformed.profile_key)
        self.assertEqual(snapshot.counts.unknown_model_count, 1)
        self.assertEqual(snapshot.counts.malformed_identity_count, 1)
        self.assertEqual(len(manager.query_log), 2)

    def test_empty_serial_or_firmware_is_malformed_identity(self):
        manager = FakeResourceManager(
            ("GPIB0::9::INSTR", "GPIB0::10::INSTR"),
            {
                "GPIB0::9::INSTR": "KEITHLEY INSTRUMENTS INC.,MODEL 6221,,D04 /700x",
                "GPIB0::10::INSTR": "KEITHLEY INSTRUMENTS INC.,MODEL 6221,4581062,",
            },
        )

        snapshot, _factory_calls = run_refresh(manager)

        self.assertEqual(
            [entry.status for entry in snapshot.entries],
            ["malformed_identity", "malformed_identity"],
        )
        self.assertTrue(all(entry.identity is None for entry in snapshot.entries))
        self.assertTrue(all(entry.profile_key is None for entry in snapshot.entries))

    def test_same_model_different_serials_reuse_the_same_profile_key(self):
        manager = FakeResourceManager(
            ("GPIB0::6::INSTR", "GPIB0::7::INSTR"),
            {
                "GPIB0::6::INSTR": IDN_2182A_6,
                "GPIB0::7::INSTR": IDN_2182A_NEW,
            },
        )
        snapshot, _factory_calls = run_refresh(manager)
        self.assertEqual([entry.profile_key for entry in snapshot.entries], ["2182a", "2182a"])
        self.assertNotEqual(snapshot.entries[0].identity.serial, snapshot.entries[1].identity.serial)

    def test_new_2182a_inventory_entry_never_grants_live(self):
        manager = FakeResourceManager(
            ("GPIB0::14::INSTR",),
            {"GPIB0::14::INSTR": IDN_2182A_NEW},
        )
        snapshot, _factory_calls = run_refresh(manager)
        self.assertEqual(snapshot.entries[0].profile_key, "2182a")
        self.assertFalse(snapshot.entries[0].live_supported)
        self.assertFalse(snapshot.as_dict()["safety"]["live_granted"])

    def test_only_exact_known_gpib25_identity_gets_default_2450_tsp_profile(self):
        manager = FakeResourceManager(
            ("GPIB0::25::INSTR", "GPIB0::26::INSTR"),
            {
                "GPIB0::25::INSTR": IDN_2450_25,
                "GPIB0::26::INSTR": IDN_2450_NEW,
            },
        )
        snapshot, _factory_calls = run_refresh(manager)
        known, future = snapshot.entries
        self.assertEqual(known.profile_key, "2450")
        self.assertEqual(known.profile_resolution, "known_tsp_asset_exact_identity")
        self.assertIsNone(future.profile_key)
        self.assertEqual(future.status, "command_set_ambiguous")
        self.assertEqual(snapshot.counts.command_set_ambiguous_count, 1)

    def test_tsp_policy_callback_can_approve_a_future_2450(self):
        manager = FakeResourceManager(
            ("GPIB0::26::INSTR",),
            {"GPIB0::26::INSTR": IDN_2450_NEW},
        )
        calls = []

        def policy(resource, identity):
            calls.append((resource, identity.serial))
            return resource == "GPIB0::26::INSTR" and identity.serial == "NEW2450"

        snapshot, _factory_calls = run_refresh(manager, tsp_policy=policy)
        self.assertEqual(calls, [("GPIB0::26::INSTR", "NEW2450")])
        self.assertEqual(snapshot.entries[0].profile_key, "2450")
        self.assertEqual(snapshot.entries[0].profile_resolution, "tsp_policy_callback")

    def test_custom_known_asset_mapping_can_approve_a_future_2450(self):
        manager = FakeResourceManager(
            ("GPIB0::26::INSTR",),
            {"GPIB0::26::INSTR": IDN_2450_NEW},
        )
        known = {
            "GPIB0::26::INSTR": inventory.KnownTspAsset(
                vendor="KEITHLEY INSTRUMENTS",
                model="2450",
                serial="NEW2450",
                firmware="2.0.0",
            )
        }
        snapshot, _factory_calls = run_refresh(manager, known_tsp_assets=known)
        self.assertEqual(snapshot.entries[0].profile_key, "2450")
        self.assertEqual(snapshot.entries[0].status, "recognized")

    def test_tsp_policy_error_fails_closed_and_refresh_continues(self):
        manager = FakeResourceManager(
            ("GPIB0::25::INSTR", "GPIB0::26::INSTR"),
            {
                "GPIB0::25::INSTR": IDN_2450_25,
                "GPIB0::26::INSTR": IDN_2450_NEW,
            },
        )

        def broken_policy(_resource, _identity):
            raise RuntimeError("policy unavailable")

        snapshot, _factory_calls = run_refresh(manager, tsp_policy=broken_policy)
        self.assertEqual(len(snapshot.entries), 2)
        for entry in snapshot.entries:
            self.assertIsNone(entry.profile_key)
            self.assertEqual(entry.status, "command_set_ambiguous")
            self.assertIn("policy unavailable", entry.error)


class SnapshotAndSimulationTests(unittest.TestCase):
    def test_handoff_simulation_is_fixture_data_not_a_real_inventory_limit(self):
        snapshot = inventory.build_simulated_inventory()
        self.assertEqual(snapshot.source, "simulate")
        self.assertEqual(len(snapshot.entries), 5)
        self.assertEqual(
            [entry.profile_key for entry in snapshot.entries],
            ["2182a", "2182a", "6221", "6221", "2450"],
        )
        self.assertEqual(snapshot.counts.recognized_profile_count, 5)
        self.assertTrue(all(not entry.live_supported for entry in snapshot.entries))

    def test_snapshot_entries_and_counts_are_immutable(self):
        snapshot = inventory.build_simulated_inventory()
        self.assertIsInstance(snapshot.entries, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.entries[0].profile_key = "changed"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.counts.filtered_gpib_count = 99

    def test_snapshot_dict_preserves_raw_identity_parse_profile_error_and_counts(self):
        manager = FakeResourceManager(
            ("TCPIP0::x::INSTR", "GPIB0::6::INSTR", "GPIB0::7::INSTR"),
            {
                "GPIB0::6::INSTR": IDN_2182A_6,
                "GPIB0::7::INSTR": TimeoutError("timeout"),
            },
        )
        snapshot, _factory_calls = run_refresh(manager)
        rendered = snapshot.as_dict()
        self.assertEqual(rendered["raw_resources"], list(manager.raw_resources))
        self.assertEqual(rendered["entries"][0]["idn_raw"], IDN_2182A_6)
        self.assertEqual(rendered["entries"][0]["identity"]["serial"], "1340129")
        self.assertEqual(rendered["entries"][0]["profile_key"], "2182a")
        self.assertIsNotNone(rendered["entries"][0]["elapsed_ms"])
        self.assertIn("TimeoutError", rendered["entries"][1]["error"])
        self.assertEqual(rendered["counts"], snapshot.counts.as_dict())


if __name__ == "__main__":
    unittest.main()
