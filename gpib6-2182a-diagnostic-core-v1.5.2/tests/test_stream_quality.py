import csv
import math
import tempfile
import unittest
from pathlib import Path

from stream_quality import analyze_stream_csv


FIELDS = (
    "elapsed_seconds",
    "voltage_v",
    "raw_response",
    "query_elapsed_ms",
)


class StreamQualityTests(unittest.TestCase):
    def test_known_case_preserves_units_shape_and_drift_sign(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "voltage-test.csv"
            with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                for elapsed, voltage, raw, latency in (
                    (0.0, 1e-6, "A", 1.0),
                    (1.0, 2e-6, "A", 2.0),
                    (2.0, 3e-6, "B", 3.0),
                    (3.0, 4e-6, "B", 4.0),
                ):
                    writer.writerow(
                        {
                            "elapsed_seconds": elapsed,
                            "voltage_v": voltage,
                            "raw_response": raw,
                            "query_elapsed_ms": latency,
                        }
                    )

            quality = analyze_stream_csv(csv_path)

        self.assertEqual(quality["sample_count"], 4)
        self.assertEqual(quality["elapsed"]["span_seconds"], 3.0)
        self.assertEqual(quality["elapsed"]["interval_seconds"]["p50"], 1.0)
        self.assertEqual(quality["query_latency_ms"]["p50"], 2.5)
        self.assertAlmostEqual(quality["query_latency_ms"]["p95"], 3.85)
        self.assertAlmostEqual(quality["voltage_v"]["median"], 2.5e-6)
        self.assertAlmostEqual(quality["voltage_v"]["mad"], 1e-6)
        self.assertAlmostEqual(
            quality["voltage_v"]["standard_deviation"],
            math.sqrt(1.25) * 1e-6,
        )
        self.assertAlmostEqual(quality["voltage_v"]["peak_to_peak"], 3e-6)
        self.assertAlmostEqual(quality["voltage_v"]["linear_drift_v_per_s"], 1e-6)
        self.assertEqual(quality["raw_response"]["consecutive_duplicate_count"], 2)
        self.assertAlmostEqual(
            quality["raw_response"]["consecutive_duplicate_fraction"],
            2 / 3,
        )
        self.assertEqual(quality["raw_response"]["longest_identical_run"], 2)

    def test_wrong_csv_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "voltage-test.csv"
            csv_path.write_text("host_time,voltage_v\nnow,1e-6\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "CSV fields"):
                analyze_stream_csv(csv_path)


if __name__ == "__main__":
    unittest.main()
