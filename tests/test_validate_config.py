#!/usr/bin/env python3
"""
Tests for scripts/validate_config.py

Each test points the validator at a temporary config.json rather than the real
one, so the repo's own settings can change freely without breaking the suite.

The cases that matter most are the silent ones: malformed JSON and wrong types
never raise in the traders, because _load_config() swallows the error and falls
back to defaults. Those tests assert the validator is loud where the traders are
quiet.

Usage:
    python3 -m unittest discover -s tests -v
"""

import os
import sys
import json
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import validate_config as vc  # noqa: E402

VALID_FASTLOOP = {
    "entry_threshold": 0.05,
    "min_momentum_pct": 0.5,
    "max_position": 5.0,
    "lookback_minutes": 5,
    "min_time_remaining": 60,
    "asset": "BTC",
    "window": "5m",
    "signal_source": "binance",
    "volume_confidence": True,
}

VALID_WEATHER = {
    "entry_threshold": 0.15,
    "exit_threshold": 0.45,
    "max_position_usd": 2.00,
    "sizing_pct": 0.05,
    "max_trades_per_run": 5,
    "locations": "NYC,Chicago,Miami",
}


class ValidatorCase(unittest.TestCase):
    """Runs validate_bot() against a temp config file."""

    def validate(self, bot, payload, raw=None):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "config.json")
        with open(path, "w") as handle:
            handle.write(raw if raw is not None else json.dumps(payload))
        with mock.patch.dict(vc.CONFIG_PATHS, {bot: path}):
            return vc.validate_bot(bot)

    def assertMentions(self, messages, needle):
        joined = " | ".join(messages).lower()
        self.assertIn(needle.lower(), joined,
                      msg=f"expected {needle!r} in {joined!r}")


class TestValidFastloop(ValidatorCase):
    def test_baseline_config_passes(self):
        result = self.validate("fastloop", VALID_FASTLOOP)
        self.assertTrue(result.ok, msg=f"unexpected errors: {result.errors}")
        self.assertEqual(result.warnings, [])

    def test_missing_key_is_only_a_note(self):
        payload = dict(VALID_FASTLOOP)
        payload.pop("asset")
        result = self.validate("fastloop", payload)
        self.assertTrue(result.ok)
        self.assertMentions(result.notes, "asset")

    def test_unknown_key_warns_that_it_is_ignored(self):
        payload = dict(VALID_FASTLOOP, nonsense_key=1)
        result = self.validate("fastloop", payload)
        self.assertTrue(result.ok)
        self.assertMentions(result.warnings, "nonsense_key")


class TestFastloopFailures(ValidatorCase):
    def test_malformed_json_is_an_error_that_names_the_fallback(self):
        result = self.validate("fastloop", None, raw='{"entry_threshold": 0.05,}')
        self.assertFalse(result.ok)
        self.assertMentions(result.errors, "invalid json")
        # The whole point: the trader would quietly use defaults instead
        self.assertMentions(result.errors, "defaults")

    def test_non_object_json_rejected(self):
        result = self.validate("fastloop", None, raw="[1, 2, 3]")
        self.assertFalse(result.ok)
        self.assertMentions(result.errors, "expected a json object")

    def test_string_where_number_expected(self):
        result = self.validate("fastloop", dict(VALID_FASTLOOP, entry_threshold="0.05"))
        self.assertFalse(result.ok)
        self.assertMentions(result.errors, "entry_threshold")

    def test_bool_is_not_accepted_as_a_number(self):
        # bool subclasses int in Python, so this needs an explicit guard
        result = self.validate("fastloop", dict(VALID_FASTLOOP, max_position=True))
        self.assertFalse(result.ok)
        self.assertMentions(result.errors, "max_position")

    def test_number_where_bool_expected(self):
        result = self.validate("fastloop", dict(VALID_FASTLOOP, volume_confidence=1))
        self.assertFalse(result.ok)
        self.assertMentions(result.errors, "volume_confidence")

    def test_entry_threshold_at_or_above_half_is_unsatisfiable(self):
        for value in (0.5, 0.75, 1.0):
            with self.subTest(value=value):
                result = self.validate("fastloop", dict(VALID_FASTLOOP, entry_threshold=value))
                self.assertFalse(result.ok)

    def test_zero_entry_threshold_rejected(self):
        result = self.validate("fastloop", dict(VALID_FASTLOOP, entry_threshold=0))
        self.assertFalse(result.ok)

    def test_lookback_of_one_disables_the_bot(self):
        # get_binance_momentum() returns None with fewer than 2 candles
        result = self.validate("fastloop", dict(VALID_FASTLOOP, lookback_minutes=1))
        self.assertFalse(result.ok)
        self.assertMentions(result.errors, "lookback_minutes")

    def test_fractional_int_rejected(self):
        result = self.validate("fastloop", dict(VALID_FASTLOOP, lookback_minutes=5.5))
        self.assertFalse(result.ok)
        self.assertMentions(result.errors, "whole number")

    def test_int_valued_float_accepted(self):
        result = self.validate("fastloop", dict(VALID_FASTLOOP, lookback_minutes=5.0))
        self.assertTrue(result.ok, msg=f"unexpected errors: {result.errors}")

    def test_unknown_asset_rejected_against_asset_symbols(self):
        result = self.validate("fastloop", dict(VALID_FASTLOOP, asset="DOGE"))
        self.assertFalse(result.ok)
        self.assertMentions(result.errors, "asset")

    def test_known_alternate_asset_accepted(self):
        result = self.validate("fastloop", dict(VALID_FASTLOOP, asset="ETH"))
        self.assertTrue(result.ok, msg=f"unexpected errors: {result.errors}")

    def test_bad_window_rejected(self):
        result = self.validate("fastloop", dict(VALID_FASTLOOP, window="1h"))
        self.assertFalse(result.ok)

    def test_bad_signal_source_rejected(self):
        result = self.validate("fastloop", dict(VALID_FASTLOOP, signal_source="kraken"))
        self.assertFalse(result.ok)

    def test_min_time_exceeding_window_skips_every_market(self):
        result = self.validate("fastloop",
                               dict(VALID_FASTLOOP, window="5m", min_time_remaining=300))
        self.assertFalse(result.ok)
        self.assertMentions(result.errors, "min_time_remaining")

    def test_min_time_valid_for_longer_window(self):
        result = self.validate("fastloop",
                               dict(VALID_FASTLOOP, window="15m", min_time_remaining=300))
        self.assertTrue(result.ok, msg=f"unexpected errors: {result.errors}")

    def test_huge_position_warns_but_passes(self):
        result = self.validate("fastloop", dict(VALID_FASTLOOP, max_position=5000.0))
        self.assertTrue(result.ok)
        self.assertMentions(result.warnings, "max_position")


class TestWeather(ValidatorCase):
    def test_baseline_config_passes(self):
        result = self.validate("weather", VALID_WEATHER)
        self.assertTrue(result.ok, msg=f"unexpected errors: {result.errors}")
        self.assertEqual(result.warnings, [])

    def test_entry_threshold_bump_still_valid(self):
        # mirrors the 0.36 -> 0.37 tuning change
        result = self.validate("weather", dict(VALID_WEATHER, entry_threshold=0.37))
        self.assertTrue(result.ok, msg=f"unexpected errors: {result.errors}")

    def test_exit_below_entry_warns(self):
        result = self.validate("weather",
                               dict(VALID_WEATHER, entry_threshold=0.50, exit_threshold=0.40))
        self.assertTrue(result.ok)
        self.assertMentions(result.warnings, "exit_threshold")

    def test_threshold_above_one_rejected(self):
        result = self.validate("weather", dict(VALID_WEATHER, entry_threshold=1.5))
        self.assertFalse(result.ok)

    def test_sizing_pct_above_one_rejected(self):
        result = self.validate("weather", dict(VALID_WEATHER, sizing_pct=1.5))
        self.assertFalse(result.ok)

    def test_zero_trades_per_run_rejected(self):
        result = self.validate("weather", dict(VALID_WEATHER, max_trades_per_run=0))
        self.assertFalse(result.ok)

    def test_empty_locations_rejected(self):
        result = self.validate("weather", dict(VALID_WEATHER, locations=""))
        self.assertFalse(result.ok)

    def test_empty_location_entry_rejected(self):
        result = self.validate("weather", dict(VALID_WEATHER, locations="NYC,,Miami"))
        self.assertFalse(result.ok)
        self.assertMentions(result.errors, "locations")

    def test_duplicate_locations_warn(self):
        result = self.validate("weather", dict(VALID_WEATHER, locations="NYC,NYC"))
        self.assertTrue(result.ok)
        self.assertMentions(result.warnings, "duplicate")


class TestMissingFile(unittest.TestCase):
    def test_absent_config_is_a_note_not_an_error(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        absent = os.path.join(directory, "does_not_exist.json")
        with mock.patch.dict(vc.CONFIG_PATHS, {"fastloop": absent}):
            result = vc.validate_bot("fastloop")
        self.assertTrue(result.ok)
        self.assertTrue(any("default" in note for note in result.notes))


class TestRealRepoConfigs(unittest.TestCase):
    """The configs actually committed here must stay valid."""

    def test_committed_configs_are_valid(self):
        for bot in sorted(vc.CONFIG_PATHS):
            with self.subTest(bot=bot):
                result = vc.validate_bot(bot)
                self.assertTrue(result.ok, msg=f"{bot}: {result.errors}")


class TestCli(unittest.TestCase):
    """Exit codes matter: doctor.py and CI both branch on them."""

    def run_cli(self, *extra):
        script = os.path.join(REPO_ROOT, "scripts", "validate_config.py")
        return subprocess.run([sys.executable, script, *extra],
                              capture_output=True, text=True, timeout=60)

    def test_exit_zero_on_valid_repo_configs(self):
        completed = self.run_cli()
        self.assertEqual(completed.returncode, 0, msg=completed.stdout + completed.stderr)

    def test_json_output_parses(self):
        completed = self.run_cli("--json")
        payload = json.loads(completed.stdout)
        self.assertEqual({row["bot"] for row in payload}, set(vc.CONFIG_PATHS))
        self.assertTrue(all(row["ok"] for row in payload))

    def test_bot_filter_limits_output(self):
        completed = self.run_cli("--bot", "weather", "--json")
        payload = json.loads(completed.stdout)
        self.assertEqual([row["bot"] for row in payload], ["weather"])

    def test_rejects_unknown_bot(self):
        completed = self.run_cli("--bot", "nope")
        self.assertNotEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
