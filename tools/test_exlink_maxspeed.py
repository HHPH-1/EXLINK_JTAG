#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import exlink_maxspeed_test as ms


class MaxspeedUnitTests(unittest.TestCase):
    def test_runtime_max_tck_calculation(self) -> None:
        self.assertEqual(ms.calculate_max_tck_hz(125_000_000, 5), 25_000_000)
        self.assertEqual(ms.calculate_max_tck_hz(120_000_000, 4), 30_000_000)

    def test_pio_divider_boundaries(self) -> None:
        divider, actual = ms.calculate_pio_divider(125_000_000, 25_000_000, 5)
        self.assertEqual(divider, 1.0)
        self.assertEqual(actual, 25_000_000)
        with self.assertRaises(ValueError):
            ms.calculate_pio_divider(125_000_000, 30_000_000, 5)
        with self.assertRaises(ValueError):
            ms.calculate_pio_divider(125_000_000, 0, 5)

    def test_top_down_frequency_sequence(self) -> None:
        self.assertEqual(ms.top_down_frequency_sequence(25_000, 20_000, 2_500), [25_000, 22_500, 20_000])
        self.assertEqual(ms.top_down_frequency_sequence(25_100, 20_000, 2_500), [25_100, 22_600, 20_100, 20_000])

    def test_binary_search_points(self) -> None:
        self.assertEqual(ms.binary_search_points(25_000, 20_000, 250), [22_500, 23_750, 24_375, 24_687, 24_843])
        with self.assertRaises(ValueError):
            ms.binary_search_points(20_000, 25_000, 250)

    def test_idcode_consistency(self) -> None:
        self.assertTrue(ms.idcodes_consistent([0x13722093] * 10))
        self.assertFalse(ms.idcodes_consistent([0x13722093, 0x23722093]))
        self.assertFalse(ms.idcodes_consistent([0xFFFFFFFF] * 10))
        self.assertFalse(ms.idcodes_consistent([0] * 10))

    def test_vivado_log_parse_and_pass_fail(self) -> None:
        text = "\n".join([
            "EXLINK_PROGRAM run=0 type=warmup status=PASS elapsed_ms=12345",
            "EXLINK_PROGRAM run=1 type=test status=PASS elapsed_ms=12003",
            "EXLINK_PROGRAM run=2 type=test status=FAIL elapsed_ms=0 reason=bad",
        ])
        runs = ms.parse_vivado_program_output(text)
        self.assertEqual(len(runs), 3)
        self.assertEqual(runs[2].reason, "bad")
        self.assertFalse(ms.all_test_runs_pass(runs))
        self.assertTrue(ms.all_test_runs_pass(runs[:2]))

    def test_statistics(self) -> None:
        summary = ms.summarize_times([4.0, 1.0, 3.0, 2.0])
        self.assertEqual(summary["min"], 1.0)
        self.assertEqual(summary["max"], 4.0)
        self.assertEqual(summary["mean"], 2.5)
        self.assertEqual(summary["median"], 2.5)
        self.assertEqual(summary["p95"], 4.0)
        self.assertGreater(summary["stdev"], 0.0)

    def test_json_csv_markdown_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp)
            result = ms.CandidateResult(
                engine="pio_safe",
                dma_chunk_bits=8192,
                requested_tck_hz=5_000_000,
                actual_tck_hz=5_000_000,
                pio_cycles_per_bit=5,
                idcode="0x13722093",
                idcode_pass_count=10,
                idcode_pass=True,
                vivado_pass=True,
                confirmed=True,
                download_times_s=[4.0, 3.5, 3.7],
                bottleneck="PIO/TCK",
            )
            env = {"maximum_theoretical_tck_hz": 25_000_000}
            ms.write_report(report_dir, [result], env, 27.245, 3.4)
            self.assertTrue((report_dir / "summary.md").exists())
            self.assertTrue((report_dir / "results.csv").exists())
            data = json.loads((report_dir / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(data[0]["dma_chunk_bits"], 8192)
            self.assertIn("Fastest median", (report_dir / "summary.md").read_text(encoding="utf-8"))

    def test_profile_parse_and_bottleneck(self) -> None:
        profile = ms.parse_profile_text("  usb_tx_space_waits=4\r\n  usb_rx_incomplete_waits=1\r\n")
        self.assertEqual(profile["usb_tx_space_waits"], 4)
        self.assertEqual(ms.infer_bottleneck(profile, 1_000_000, 10_000_000), "USB TX")

    def test_force_tck_behavior_model(self) -> None:
        requested_ns = 100
        force_hz = 7_000_000
        actual_ns = ms.math.floor(1_000_000_000 / force_hz)
        self.assertNotEqual(requested_ns, actual_ns)

    def test_non_byte_shift_and_max_shift_lengths(self) -> None:
        self.assertEqual((7 + 7) // 8, 1)
        self.assertEqual((131072 + 7) // 8, 16384)

    def test_recovery_failure_is_not_pass(self) -> None:
        result = ms.CandidateResult("pio_safe", 8192, 25_000_000, 0, 5, failure_reason="recovery failed")
        self.assertFalse(result.vivado_pass)
        self.assertFalse(result.confirmed)


if __name__ == "__main__":
    unittest.main()
