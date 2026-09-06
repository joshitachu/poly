#!/usr/bin/env python3
"""
Tests for scripts/backtest_momentum.py

The backtest is only useful if it computes momentum the same way the live
trader does. These tests pin that: momentum_at() is checked against the exact
formula in fastloop_trader.get_binance_momentum() (open of the oldest candle in
the window -> close of the newest, volume_ratio = newest volume / window avg),
including the detail that a flat window is labelled "down", not "neutral".

The rest pins the entry/exit alignment in evaluate(). An off-by-one there is
invisible in the output - the table still prints plausible hit rates - but it
would be measuring a different horizon than the one in the header, which is
exactly the kind of silently-wrong number a threshold gets tuned on.

Everything here runs offline on synthetic klines; nothing calls Binance.

Usage:
    python3 -m unittest discover -s tests -v
"""

import os
import sys
import json
import shutil
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import backtest_momentum as bm  # noqa: E402

OPEN_TIME = 1_700_000_000_000  # arbitrary fixed epoch ms; nothing depends on the date


def kline(index, open_price, close_price, volume=1.0):
    """One Binance 1m kline. Binance returns numbers as strings - so do we."""
    high = max(open_price, close_price)
    low = min(open_price, close_price)
    return [
        OPEN_TIME + index * 60_000,
        f"{open_price}", f"{high}", f"{low}", f"{close_price}", f"{volume}",
        OPEN_TIME + (index + 1) * 60_000 - 1,
        "0.0", 0, "0.0", "0.0", "0",
    ]


def series(prices, volumes=None):
    """Build candles where candle i opens at prices[i] and closes at prices[i+1]."""
    volumes = volumes or [1.0] * (len(prices) - 1)
    return [kline(i, prices[i], prices[i + 1], volumes[i])
            for i in range(len(prices) - 1)]


class TestMomentumAt(unittest.TestCase):
    def test_matches_the_traders_formula(self):
        # open of the oldest candle is 100, close of the newest is 101 -> +1%
        window = [kline(0, 100, 100.5), kline(1, 100.5, 101)]
        signal = bm.momentum_at(window)
        self.assertAlmostEqual(signal["momentum_pct"], 1.0)
        self.assertEqual(signal["direction"], "up")
        self.assertAlmostEqual(signal["price_now"], 101.0)

    def test_ignores_intermediate_candles(self):
        # A spike in the middle must not move the reading: only the first open
        # and the last close matter, same as get_binance_momentum().
        calm = [kline(0, 100, 100), kline(1, 100, 100), kline(2, 100, 99)]
        spiky = [kline(0, 100, 250), kline(1, 250, 3), kline(2, 3, 99)]
        self.assertAlmostEqual(bm.momentum_at(calm)["momentum_pct"],
                               bm.momentum_at(spiky)["momentum_pct"])

    def test_negative_momentum_is_down(self):
        window = [kline(0, 100, 99), kline(1, 99, 98)]
        self.assertEqual(bm.momentum_at(window)["direction"], "down")

    def test_flat_window_is_labelled_down(self):
        # The trader uses `"up" if momentum_pct > 0 else "down"`, so exactly
        # zero falls to "down". Mirrored deliberately - if the trader ever
        # grows a neutral case, this test should fail and be updated with it.
        window = [kline(0, 100, 100), kline(1, 100, 100)]
        self.assertEqual(bm.momentum_at(window)["direction"], "down")
        self.assertEqual(bm.momentum_at(window)["momentum_pct"], 0.0)

    def test_volume_ratio_is_newest_over_window_average(self):
        window = [kline(0, 100, 100, volume=1.0), kline(1, 100, 100, volume=3.0)]
        # avg = 2.0, newest = 3.0
        self.assertAlmostEqual(bm.momentum_at(window)["volume_ratio"], 1.5)

    def test_zero_volume_window_does_not_divide_by_zero(self):
        window = [kline(0, 100, 100, volume=0.0), kline(1, 100, 100, volume=0.0)]
        self.assertEqual(bm.momentum_at(window)["volume_ratio"], 1.0)

    def test_zero_open_returns_none(self):
        window = [kline(0, 0, 100), kline(1, 100, 101)]
        self.assertIsNone(bm.momentum_at(window))


class TestBaseRate(unittest.TestCase):
    def test_monotonic_rise(self):
        candles = series([100, 101, 102, 103, 104])
        base = bm.base_rate(candles, horizon=1)
        self.assertEqual(base["up_rate"], 1.0)
        self.assertEqual(base["flat_rate"], 0.0)

    def test_flat_market_counts_as_flat_not_up(self):
        candles = series([100] * 6)
        base = bm.base_rate(candles, horizon=2)
        self.assertEqual(base["up_rate"], 0.0)
        self.assertEqual(base["flat_rate"], 1.0)

    def test_sample_count_shrinks_with_horizon(self):
        candles = series([100, 101, 102, 103, 104, 105])  # 5 candles
        self.assertEqual(bm.base_rate(candles, horizon=1)["samples"], 4)
        self.assertEqual(bm.base_rate(candles, horizon=3)["samples"], 2)

    def test_too_short_for_horizon(self):
        self.assertIsNone(bm.base_rate(series([100, 101]), horizon=5))


class TestEvaluate(unittest.TestCase):
    # closes:      c0=100  c1=101  c2=101  c3=102  c4=100  c5=100
    # lookback 2, horizon 2:
    #   i=2 window (c0,c1): open 100 -> close 101 = +1.0% up
    #                       entry close(c1)=101, exit close(c3)=102  -> win
    #   i=3 window (c1,c2): open 100 -> close 101 = +1.0% up
    #                       entry close(c2)=101, exit close(c4)=100  -> loss
    PRICES = [100, 100, 101, 101, 102, 100, 100]

    def candles(self, volumes=None):
        return series(self.PRICES, volumes)

    def test_entry_and_exit_alignment(self):
        stats = bm.evaluate(self.candles(), threshold=0.5, lookback=2,
                            horizon=2, volume_gate=False)
        self.assertEqual(stats["signals"], 2)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 1)
        self.assertEqual(stats["hit_rate"], 0.5)

    def test_threshold_above_every_signal_yields_nothing(self):
        stats = bm.evaluate(self.candles(), threshold=5.0, lookback=2,
                            horizon=2, volume_gate=False)
        self.assertEqual(stats["signals"], 0)
        self.assertIsNone(stats["hit_rate"])
        self.assertIsNone(stats["avg_move_pct"])

    def test_threshold_is_on_absolute_momentum(self):
        # Falling market: the signal must still fire, and a continued fall is a
        # win because the move is scored in the signal's own direction.
        candles = series([100, 100, 99, 99, 98, 97, 97])
        stats = bm.evaluate(candles, threshold=0.5, lookback=2, horizon=2,
                            volume_gate=False)
        self.assertGreater(stats["signals"], 0)
        self.assertEqual(stats["losses"], 0)
        self.assertGreater(stats["avg_move_pct"], 0)

    def test_volume_gate_filters_thin_candles(self):
        # Window (c0,c1) has volumes [10, 0.1] -> the newest candle is 0.02x
        # the window average, under the trader's 0.5x floor, so that signal is
        # dropped. Window (c1,c2) is [0.1, 0.1] -> ratio 1.0, and survives.
        volumes = [10.0, 0.1, 0.1, 0.1, 0.1, 0.1]
        gated = bm.evaluate(self.candles(volumes), threshold=0.5, lookback=2,
                            horizon=2, volume_gate=True)
        ungated = bm.evaluate(self.candles(volumes), threshold=0.5, lookback=2,
                              horizon=2, volume_gate=False)
        self.assertEqual(gated["signals"], 1)
        self.assertEqual(gated["filtered_low_volume"], 1)
        self.assertEqual(ungated["signals"], 2)
        self.assertEqual(ungated["filtered_low_volume"], 0)
        # The gate only removes signals; it never invents them.
        self.assertEqual(gated["signals"] + gated["filtered_low_volume"],
                         ungated["signals"])

    def test_flat_outcomes_are_excluded_from_hit_rate(self):
        # Price moves during the lookback, then goes nowhere over the horizon.
        candles = series([100, 100, 102, 102, 102, 102, 102])
        stats = bm.evaluate(candles, threshold=0.5, lookback=2, horizon=2,
                            volume_gate=False)
        self.assertEqual(stats["flat"], stats["signals"])
        self.assertIsNone(stats["hit_rate"])

    def test_moves_are_not_leaked_into_the_result(self):
        # The per-signal move list is an implementation detail; --json output
        # would balloon if it ever started riding along.
        stats = bm.evaluate(self.candles(), threshold=0.5, lookback=2,
                            horizon=2, volume_gate=False)
        self.assertNotIn("moves", stats)


class TestParseThresholds(unittest.TestCase):
    def test_default_when_unset(self):
        self.assertEqual(bm.parse_thresholds(None), bm.DEFAULT_THRESHOLDS)
        self.assertEqual(bm.parse_thresholds(""), bm.DEFAULT_THRESHOLDS)

    def test_sorted_and_deduped(self):
        self.assertEqual(bm.parse_thresholds("0.5, 0.1,0.5 , 0.25"), [0.1, 0.25, 0.5])

    def test_trailing_comma_is_tolerated(self):
        self.assertEqual(bm.parse_thresholds("0.25,"), [0.25])

    def test_rejects_non_numeric(self):
        with self.assertRaises(ValueError):
            bm.parse_thresholds("0.25,high")

    def test_rejects_negative(self):
        with self.assertRaises(ValueError):
            bm.parse_thresholds("-0.5")

    def test_rejects_empty_list(self):
        with self.assertRaises(ValueError):
            bm.parse_thresholds(",,")


class TestLoadCandles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="poly-backtest-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, content):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as handle:
            handle.write(content)
        return path

    def test_reads_a_saved_sweep(self):
        candles = series([100, 101, 102])
        path = self.write("candles.json", json.dumps(candles))
        self.assertEqual(len(bm.load_candles(path)), 2)

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            bm.load_candles(os.path.join(self.tmp, "nope.json"))

    def test_invalid_json_exits(self):
        path = self.write("broken.json", "[[1,2,3],")
        with self.assertRaises(SystemExit):
            bm.load_candles(path)

    def test_empty_array_exits(self):
        path = self.write("empty.json", "[]")
        with self.assertRaises(SystemExit):
            bm.load_candles(path)

    def test_short_rows_exit(self):
        # Six fields is the minimum: open time through volume.
        path = self.write("short.json", json.dumps([[OPEN_TIME, "1", "1", "1", "1"]]))
        with self.assertRaises(SystemExit):
            bm.load_candles(path)


class TestFormatReport(unittest.TestCase):
    META = {
        "symbol": "BTCUSDT", "asset": "BTC", "days": 3.0, "candles": 4320,
        "lookback": 5, "horizon": 5, "window": "5m", "volume_gate": True,
        "first": "2026-08-24 00:00", "last": "2026-08-27 00:00",
    }

    def rows(self):
        return [
            {"threshold": 0.25, "signals": 120, "wins": 70, "losses": 50, "flat": 0,
             "filtered_low_volume": 8, "hit_rate": 0.5833, "avg_move_pct": 0.041},
            {"threshold": 5.0, "signals": 0, "wins": 0, "losses": 0, "flat": 0,
             "filtered_low_volume": 0, "hit_rate": None, "avg_move_pct": None},
        ]

    def test_marks_the_configured_threshold(self):
        text = bm.format_report(self.META, {"samples": 4300, "up_rate": 0.51,
                                            "flat_rate": 0.02},
                                self.rows(), configured=0.25)
        self.assertIn("<- config", text)
        # 58.3% hit rate against a 51% base rate is a +7.3pp edge
        self.assertIn("+7.3pp", text)

    def test_empty_row_renders_as_na_not_a_crash(self):
        text = bm.format_report(self.META, None, self.rows(), configured=None)
        self.assertIn("n/a", text)
        self.assertNotIn("<- config", text)


if __name__ == "__main__":
    unittest.main()
