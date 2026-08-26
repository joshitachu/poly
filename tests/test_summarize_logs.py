#!/usr/bin/env python3
"""
Tests for scripts/summarize_logs.py

The fixtures below reproduce the exact lines fastloop_trader.py and
weather_trader.py print (see their summary blocks). That is the point of these
tests: if someone rewords a summary line, the parser silently stops counting
trades, and only a fixture pinned to the real format catches it.

Usage:
    python3 -m unittest discover -s tests -v
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import summarize_logs as sl  # noqa: E402


# Exactly as fastloop_trader.py emits it, run.sh header included
FASTLOOP_LOG = """--- 2026-08-26 10:00:01 UTC ---

\U0001F4CA Summary: No trade (momentum too weak: 0.123%)

--- 2026-08-26 10:05:01 UTC ---

\U0001F4CA Summary:
  Sprint: Bitcoin Up or Down - 10:10 AM ET
  Signal: UP 0.812% | YES $0.470
  Action: TRADED

--- 2026-08-26 10:10:02 UTC ---

\U0001F4CA Summary: No trade (momentum too weak: 0.045%)

--- 2026-08-26 10:15:01 UTC ---
\U0001F4CA Summary: No tradeable fast_markets (too close to expiry)

--- 2026-08-26 10:20:01 UTC ---

\U0001F4CA Summary:
  Sprint: Bitcoin Up or Down - 10:25 AM ET
  Signal: DOWN -0.640% | YES $0.510
  Action: FAILED
  ❌ Trade failed: insufficient balance
"""

# Exactly as weather_trader.py emits it
WEATHER_LOG = """--- 2026-08-26 09:30:04 UTC ---
==================================================
Summary:
  Events scanned: 12
  Entry opportunities: 3
  Exit opportunities:  1
  Trades executed:     2

  [PAPER MODE - trades simulated with real prices]

--- 2026-08-26 10:00:03 UTC ---
==================================================
Summary:
  Events scanned: 11
  Entry opportunities: 0
  Exit opportunities:  0
  Trades executed:     0
"""


class TempLogMixin:
    def write_log(self, text, name="test.log"):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path


class TestParseSince(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(sl.parse_since(None))
        self.assertIsNone(sl.parse_since(""))

    def test_units(self):
        now = datetime.now(timezone.utc)
        for spec, delta in [("90m", timedelta(minutes=90)),
                            ("24h", timedelta(hours=24)),
                            ("7d", timedelta(days=7))]:
            cutoff = sl.parse_since(spec)
            self.assertAlmostEqual((now - cutoff).total_seconds(),
                                   delta.total_seconds(), delta=5,
                                   msg=f"for {spec}")

    def test_case_and_whitespace_tolerated(self):
        self.assertIsNotNone(sl.parse_since(" 24H "))

    def test_rejects_garbage(self):
        for bad in ["5x", "hours", "-3h", "3.5h", "h24", "24"]:
            with self.subTest(spec=bad):
                with self.assertRaises(ValueError):
                    sl.parse_since(bad)


class TestSplitRuns(unittest.TestCase):
    def test_splits_on_run_sh_header(self):
        runs = list(sl.split_runs(FASTLOOP_LOG.splitlines(keepends=True)))
        self.assertEqual(len(runs), 5)

    def test_timestamps_are_utc_aware(self):
        first_ts, _ = next(iter(sl.split_runs(FASTLOOP_LOG.splitlines(keepends=True))))
        self.assertEqual(first_ts,
                         datetime(2026, 8, 26, 10, 0, 1, tzinfo=timezone.utc))
        self.assertIsNotNone(first_ts.tzinfo)

    def test_leading_output_without_header_is_kept(self):
        # A log truncated mid-run should not lose its first block
        runs = list(sl.split_runs(["orphan line\n", "--- 2026-08-26 10:00:01 UTC ---\n", "x\n"]))
        self.assertEqual(len(runs), 2)
        self.assertIsNone(runs[0][0])

    def test_empty_input(self):
        self.assertEqual(list(sl.split_runs([])), [])


class TestNormalizeReason(unittest.TestCase):
    def test_strips_numeric_detail_so_reasons_group(self):
        self.assertEqual(sl.normalize_reason("momentum too weak: 0.123%"),
                         "momentum too weak")
        self.assertEqual(sl.normalize_reason("momentum too weak: 9.999%"),
                         "momentum too weak")

    def test_lowercases_and_trims(self):
        self.assertEqual(sl.normalize_reason("  Fees Eat The Edge "), "fees eat the edge")

    def test_empty_becomes_unspecified(self):
        self.assertEqual(sl.normalize_reason(None), "unspecified")
        self.assertEqual(sl.normalize_reason(""), "unspecified")


class TestSummarizeRun(unittest.TestCase):
    def test_counts_traded_action(self):
        run = sl.summarize_run(["  Action: TRADED\n"])
        self.assertEqual(run["trades"], 1)
        self.assertEqual(run["action"], "TRADED")

    def test_dry_run_is_not_a_trade(self):
        run = sl.summarize_run(["  Action: DRY RUN\n"])
        self.assertEqual(run["trades"], 0)
        self.assertEqual(run["action"], "DRY RUN")

    def test_failed_action_is_not_a_trade(self):
        run = sl.summarize_run(["  Action: FAILED\n"])
        self.assertEqual(run["trades"], 0)
        self.assertEqual(run["action"], "FAILED")

    def test_extracts_no_trade_reason(self):
        run = sl.summarize_run(["\U0001F4CA Summary: No trade (low volume)\n"])
        self.assertEqual(run["reason"], "low volume")

    def test_extracts_reason_without_parentheses(self):
        run = sl.summarize_run(["\U0001F4CA Summary: No markets available\n"])
        self.assertEqual(run["reason"], "no markets")

    def test_signal_percent_is_absolute(self):
        run = sl.summarize_run(["  Signal: DOWN -0.640% | YES $0.510\n"])
        self.assertAlmostEqual(run["signal_pct"], 0.640)

    def test_weather_trades_counted_from_summary_block(self):
        run = sl.summarize_run([
            "  Events scanned: 12\n",
            "  Entry opportunities: 3\n",
            "  Exit opportunities:  1\n",
            "  Trades executed:     2\n",
        ])
        self.assertEqual(run["trades"], 2)
        self.assertEqual(run["weather"]["Events scanned"], 12)

    def test_errors_collected(self):
        run = sl.summarize_run(["  ❌ Trade failed: insufficient balance\n"])
        self.assertEqual(len(run["errors"]), 1)

    def test_unrelated_lines_ignored(self):
        run = sl.summarize_run(["\U0001F9E0 Analyzing...\n", "==========\n", "\n"])
        self.assertEqual(run["trades"], 0)
        self.assertIsNone(run["reason"])
        self.assertEqual(run["errors"], [])


class TestSummarizeLog(TempLogMixin, unittest.TestCase):
    def test_fastloop_aggregates(self):
        report = sl.summarize_log(self.write_log(FASTLOOP_LOG), None)
        self.assertEqual(report["runs"], 5)
        self.assertEqual(report["trades"], 1)
        self.assertEqual(report["failures"], 1)
        self.assertEqual(report["reasons"]["momentum too weak"], 2)
        self.assertEqual(report["reasons"]["too close to expiry"], 1)
        self.assertEqual(len(report["errors"]), 1)

    def test_weather_aggregates(self):
        report = sl.summarize_log(self.write_log(WEATHER_LOG), None)
        self.assertEqual(report["runs"], 2)
        self.assertEqual(report["trades"], 2)
        self.assertEqual(report["events_scanned"], 23)

    def test_since_filter_excludes_older_runs(self):
        path = self.write_log(FASTLOOP_LOG)
        cutoff = datetime(2026, 8, 26, 10, 12, tzinfo=timezone.utc)
        report = sl.summarize_log(path, cutoff)
        self.assertEqual(report["runs"], 2)
        self.assertEqual(report["runs_skipped_by_filter"], 3)

    def test_future_cutoff_excludes_everything(self):
        report = sl.summarize_log(self.write_log(FASTLOOP_LOG),
                                  datetime.now(timezone.utc))
        self.assertEqual(report["runs"], 0)
        self.assertEqual(report["trades"], 0)

    def test_window_bounds(self):
        report = sl.summarize_log(self.write_log(FASTLOOP_LOG), None)
        self.assertEqual(report["first_run"].hour, 10)
        self.assertEqual(report["first_run"].minute, 0)
        self.assertEqual(report["last_run"].minute, 20)

    def test_empty_log_is_not_an_error(self):
        report = sl.summarize_log(self.write_log(""), None)
        self.assertEqual(report["runs"], 0)

    def test_malformed_log_does_not_crash(self):
        report = sl.summarize_log(self.write_log("garbage\n\n???\n"), None)
        self.assertEqual(report["runs"], 1)
        self.assertEqual(report["trades"], 0)


class TestFormatting(TempLogMixin, unittest.TestCase):
    def test_text_report_mentions_key_numbers(self):
        report = sl.summarize_log(self.write_log(FASTLOOP_LOG), None)
        text = sl.format_report(report)
        self.assertIn("Runs:", text)
        self.assertIn("momentum too weak", text)
        self.assertIn("No-trade reasons:", text)

    def test_empty_report_says_no_runs(self):
        report = sl.summarize_log(self.write_log(""), None)
        self.assertIn("No runs in range", sl.format_report(report))

    def test_jsonable_is_serializable(self):
        report = sl.summarize_log(self.write_log(FASTLOOP_LOG), None)
        payload = sl.jsonable(report)
        json.dumps(payload)  # must not raise on datetimes
        self.assertIsInstance(payload["first_run"], str)
        self.assertNotIn("signals", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
