#!/usr/bin/env python3
"""
Tests for tradejournal.py and scripts/journal_report.py

The kwargs in the fixtures below are copied from the three real log_trade()
call sites: fastloop_trader.py (entry, with momentum fields),
weather_trader.py (entry, with location/metric fields) and weather_trader.py
(exit, action="sell" and no confidence). If a trader starts passing something
new, the "extra" passthrough should absorb it without a change here.

Usage:
    python3 -m unittest discover -s tests -v
"""

import io
import os
import sys
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone, timedelta
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import tradejournal as tj  # noqa: E402
import journal_report as jr  # noqa: E402

# fastloop_trader.py:732
FASTLOOP_CALL = dict(
    trade_id="fl-1",
    source="sdk:fastloop",
    thesis="BTC up +0.812% but YES only $0.470",
    confidence=0.62,
    asset="BTC",
    momentum_pct=0.812,
    volume_ratio=1.34,
    signal_source="binance",
)

# weather_trader.py:811
WEATHER_ENTRY_CALL = dict(
    trade_id="wx-1",
    source="sdk:weather",
    thesis="NOAA forecasts 88F for NYC on 2026-08-27, bucket '87-89' underpriced at $0.31",
    confidence=0.68,
    location="NYC",
    forecast_temp=88,
    target_date="2026-08-27",
    metric="high_temp",
)

# weather_trader.py:603 - note: no confidence, and action="sell"
WEATHER_EXIT_CALL = dict(
    trade_id="wx-2",
    source="sdk:weather",
    thesis="Exit: price $0.52 reached exit threshold $0.45",
    action="sell",
)


class JournalCase(unittest.TestCase):
    """Redirects the journal to a temp file for each test."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.path = os.path.join(self.directory, "trades.jsonl")
        patcher = mock.patch.dict(os.environ, {"SIMMER_JOURNAL_PATH": self.path})
        patcher.start()
        self.addCleanup(patcher.stop)

    def lines(self):
        with open(self.path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


class TestBuildRecord(unittest.TestCase):
    def test_core_fields_promoted_to_top_level(self):
        record = tj.build_record(**FASTLOOP_CALL)
        for key in ("trade_id", "source", "thesis", "confidence"):
            self.assertIn(key, record)

    def test_unknown_fields_go_to_extra(self):
        record = tj.build_record(**FASTLOOP_CALL)
        self.assertEqual(record["extra"]["asset"], "BTC")
        self.assertEqual(record["extra"]["momentum_pct"], 0.812)
        self.assertNotIn("asset", record)

    def test_action_defaults_to_buy(self):
        self.assertEqual(tj.build_record(**FASTLOOP_CALL)["action"], "buy")

    def test_explicit_action_preserved(self):
        self.assertEqual(tj.build_record(**WEATHER_EXIT_CALL)["action"], "sell")

    def test_timestamp_is_utc_iso(self):
        record = tj.build_record(**FASTLOOP_CALL)
        parsed = datetime.fromisoformat(record["ts"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_positional_args_preserved_not_dropped(self):
        # The old no-op fallback accepted *args, so they must not vanish silently
        record = tj.build_record("legacy", 42, trade_id="x")
        self.assertEqual(len(record["args"]), 2)

    def test_no_extra_key_when_only_core_fields(self):
        record = tj.build_record(trade_id="x", source="s", thesis="t")
        self.assertNotIn("extra", record)


class TestLogTrade(JournalCase):
    def test_writes_one_line_per_call(self):
        self.assertTrue(tj.log_trade(**FASTLOOP_CALL))
        self.assertTrue(tj.log_trade(**WEATHER_ENTRY_CALL))
        self.assertEqual(len(self.lines()), 2)

    def test_creates_missing_directory(self):
        nested = os.path.join(self.directory, "deep", "logs", "trades.jsonl")
        with mock.patch.dict(os.environ, {"SIMMER_JOURNAL_PATH": nested}):
            self.assertTrue(tj.log_trade(**FASTLOOP_CALL))
        self.assertTrue(os.path.isfile(nested))

    def test_appends_rather_than_truncates(self):
        tj.log_trade(**FASTLOOP_CALL)
        tj.log_trade(**FASTLOOP_CALL)
        tj.log_trade(**FASTLOOP_CALL)
        self.assertEqual(len(self.lines()), 3)

    def test_each_line_is_standalone_json(self):
        tj.log_trade(**WEATHER_ENTRY_CALL)
        tj.log_trade(**WEATHER_EXIT_CALL)
        for record in self.lines():
            self.assertIsInstance(record, dict)

    def test_unserializable_value_does_not_raise(self):
        # default=str should absorb odd objects rather than blowing up a trade
        self.assertTrue(tj.log_trade(trade_id="x", source="s", weird=object()))

    def test_returns_false_and_warns_when_path_unwritable(self):
        # A directory where the file should be: open() must fail
        blocked = os.path.join(self.directory, "blocked")
        os.makedirs(blocked, exist_ok=True)
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"SIMMER_JOURNAL_PATH": blocked}):
            with redirect_stderr(stderr):
                result = tj.log_trade(**FASTLOOP_CALL)
        self.assertFalse(result)
        self.assertIn("journal", stderr.getvalue().lower())

    def test_never_raises_even_on_internal_failure(self):
        # The contract that matters: the trade already executed
        with mock.patch.object(tj, "build_record", side_effect=RuntimeError("boom")):
            with redirect_stderr(io.StringIO()):
                self.assertFalse(tj.log_trade(**FASTLOOP_CALL))


class TestReadTrades(JournalCase):
    def test_missing_file_is_empty_not_an_error(self):
        records, bad = tj.read_trades(os.path.join(self.directory, "nope.jsonl"))
        self.assertEqual((records, bad), ([], 0))

    def test_round_trip(self):
        tj.log_trade(**FASTLOOP_CALL)
        records, bad = tj.read_trades()
        self.assertEqual(bad, 0)
        self.assertEqual(records[0]["trade_id"], "fl-1")

    def test_truncated_line_counted_not_fatal(self):
        tj.log_trade(**FASTLOOP_CALL)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write('{"trade_id": "half-writ')
        records, bad = tj.read_trades()
        self.assertEqual(len(records), 1)
        self.assertEqual(bad, 1)

    def test_blank_lines_ignored(self):
        tj.log_trade(**FASTLOOP_CALL)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write("\n\n")
        records, bad = tj.read_trades()
        self.assertEqual((len(records), bad), (1, 0))

    def test_non_object_line_counted_as_bad(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("[1,2,3]\n")
        records, bad = tj.read_trades()
        self.assertEqual((records, bad), ([], 1))


class TestJournalPath(unittest.TestCase):
    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"SIMMER_JOURNAL_PATH": "/tmp/custom.jsonl"}):
            self.assertEqual(tj.journal_path(), "/tmp/custom.jsonl")

    def test_default_is_under_logs(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(tj.journal_path().endswith(os.path.join("logs", "trades.jsonl")))


class TestReportSummarize(unittest.TestCase):
    def records(self):
        return [tj.build_record(**call) for call in
                (FASTLOOP_CALL, WEATHER_ENTRY_CALL, WEATHER_EXIT_CALL)]

    def test_groups_by_source(self):
        groups = jr.summarize(self.records())
        self.assertEqual([g["source"] for g in groups], ["sdk:fastloop", "sdk:weather"])

    def test_counts_buys_and_sells(self):
        weather = next(g for g in jr.summarize(self.records()) if g["source"] == "sdk:weather")
        self.assertEqual((weather["buys"], weather["sells"]), (1, 1))

    def test_confidence_averaged_over_records_that_have_it(self):
        weather = next(g for g in jr.summarize(self.records()) if g["source"] == "sdk:weather")
        # only the entry carries confidence; the exit must not count as 0.0
        self.assertEqual(len(weather["confidences"]), 1)
        self.assertAlmostEqual(weather["confidences"][0], 0.68)

    def test_numeric_extras_averaged(self):
        fastloop = next(g for g in jr.summarize(self.records()) if g["source"] == "sdk:fastloop")
        self.assertAlmostEqual(fastloop["extras"]["momentum_pct"]["numeric"][0], 0.812)

    def test_string_extras_counted(self):
        weather = next(g for g in jr.summarize(self.records()) if g["source"] == "sdk:weather")
        self.assertEqual(weather["extras"]["location"]["values"]["NYC"], 1)

    def test_missing_source_becomes_unknown(self):
        groups = jr.summarize([{"trade_id": "x"}])
        self.assertEqual(groups[0]["source"], "unknown")

    def test_missing_thesis_counted(self):
        groups = jr.summarize([{"source": "s", "trade_id": "x"}])
        self.assertEqual(groups[0]["missing_thesis"], 1)

    def test_bool_confidence_not_treated_as_number(self):
        groups = jr.summarize([{"source": "s", "confidence": True}])
        self.assertEqual(groups[0]["confidences"], [])

    def test_format_and_jsonable_do_not_crash(self):
        for group in jr.summarize(self.records()):
            self.assertIn(group["source"], jr.format_group(group))
            json.dumps(jr.jsonable(group))


class TestReportTimeHandling(unittest.TestCase):
    def test_naive_timestamp_treated_as_utc(self):
        stamp = jr.record_time({"ts": "2026-08-26T10:00:00"})
        self.assertEqual(stamp.tzinfo, timezone.utc)

    def test_bad_timestamp_returns_none(self):
        for bad in ["not-a-date", "", None, 12345]:
            with self.subTest(value=bad):
                self.assertIsNone(jr.record_time({"ts": bad}))

    def test_missing_ts_returns_none(self):
        self.assertIsNone(jr.record_time({}))

    def test_parse_since_matches_summarizer_semantics(self):
        now = datetime.now(timezone.utc)
        self.assertAlmostEqual((now - jr.parse_since("24h")).total_seconds(),
                               timedelta(hours=24).total_seconds(), delta=5)

    def test_parse_since_rejects_garbage(self):
        with self.assertRaises(ValueError):
            jr.parse_since("soon")


class TestTraderIntegration(JournalCase):
    """The traders must actually reach this module now, not the no-op."""

    def test_fastloop_imports_a_working_log_trade(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        from lib import load_trader_module
        module = load_trader_module("fastloop")
        self.assertTrue(module.JOURNAL_AVAILABLE,
                        "fastloop fell back to the no-op log_trade")
        self.assertTrue(module.log_trade(**FASTLOOP_CALL))
        self.assertEqual(len(self.lines()), 1)

    def test_weather_imports_a_working_log_trade(self):
        from lib import load_trader_module
        module = load_trader_module("weather")
        self.assertTrue(module.JOURNAL_AVAILABLE,
                        "weather fell back to the no-op log_trade")
        self.assertTrue(module.log_trade(**WEATHER_ENTRY_CALL))
        self.assertEqual(len(self.lines()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
