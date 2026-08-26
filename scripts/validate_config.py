#!/usr/bin/env python3
"""
Config Validator

Checks config.json (fastloop) and weather-trader/config.json (weather) against
the CONFIG_SCHEMA each trader actually uses, plus range rules that the schema
can't express.

Worth running before any cron deploy, because the traders fail quietly:
_load_config() catches JSONDecodeError and falls back to built-in defaults. A
stray comma means the bot keeps trading with parameters that don't match the
file you edited, and nothing in the logs says so.

Usage:
    python3 scripts/validate_config.py
    python3 scripts/validate_config.py --bot fastloop
    python3 scripts/validate_config.py --json
"""

import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import CONFIG_PATHS, display_path, load_trader_module  # noqa: E402

sys.stdout.reconfigure(line_buffering=True)

# Bounds the trader schemas don't encode. (low, high) are inclusive; None = open.
# "warn_above" flags legal-but-suspicious values instead of failing.
RULES = {
    "fastloop": {
        # Divergence from 50¢, so >= 0.5 can never be satisfied by a real price
        "entry_threshold": {"low": 0.0, "high": 0.5, "exclusive": True, "warn_above": 0.25},
        "min_momentum_pct": {"low": 0.0, "high": 100.0, "warn_above": 5.0},
        "max_position": {"low": 0.0, "high": None, "exclusive": True, "warn_above": 100.0},
        # get_binance_momentum() passes this as Binance's `limit` and returns
        # None when fewer than 2 candles come back, so 1 disables the bot
        "lookback_minutes": {"low": 2, "high": 1000},
        "min_time_remaining": {"low": 0, "high": 3600},
        "daily_budget": {"low": 0.0, "high": None, "exclusive": True, "warn_above": 1000.0},
        "asset": {"choices_from": "ASSET_SYMBOLS"},
        "window": {"choices": ["5m", "15m"]},
        "signal_source": {"choices": ["binance", "coingecko"]},
    },
    "weather": {
        "entry_threshold": {"low": 0.0, "high": 1.0, "exclusive": True},
        "exit_threshold": {"low": 0.0, "high": 1.0, "exclusive": True},
        "max_position_usd": {"low": 0.0, "high": None, "exclusive": True, "warn_above": 100.0},
        "sizing_pct": {"low": 0.0, "high": 1.0, "exclusive": True, "warn_above": 0.5},
        "max_trades_per_run": {"low": 1, "high": 100},
    },
}


class Result:
    """Collects errors (exit 1) and warnings (exit 0) for one config file."""

    def __init__(self, bot, path):
        self.bot = bot
        self.path = path
        self.errors = []
        self.warnings = []
        self.notes = []

    @property
    def ok(self):
        return not self.errors

    @property
    def config_display(self):
        return display_path(self.path)

    def as_dict(self):
        return {
            "bot": self.bot,
            "config": display_path(self.path),
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "notes": self.notes,
        }


def type_name(spec_type):
    return getattr(spec_type, "__name__", str(spec_type))


def check_type(key, value, expected, result):
    """Type-check one value the way the trader would use it. True if usable."""
    if expected is bool:
        if not isinstance(value, bool):
            result.errors.append(f"{key}: expected true/false, got {value!r}")
            return False
        return True

    if expected is str:
        if not isinstance(value, str):
            result.errors.append(f"{key}: expected a string, got {value!r}")
            return False
        if not value.strip():
            result.errors.append(f"{key}: is empty")
            return False
        return True

    # bool is a subclass of int in Python, so reject it explicitly for numbers
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        result.errors.append(f"{key}: expected {type_name(expected)}, got {value!r}")
        return False

    if expected is int and isinstance(value, float) and not value.is_integer():
        result.errors.append(f"{key}: expected a whole number, got {value!r}")
        return False

    return True


def check_range(key, value, rule, module, result):
    """Apply the bounds/choices rules above to an already type-checked value."""
    choices = rule.get("choices")
    if rule.get("choices_from"):
        choices = sorted(getattr(module, rule["choices_from"], {}) or []) or choices
    if choices:
        if value not in choices:
            result.errors.append(f"{key}: {value!r} is not one of {', '.join(map(str, choices))}")
        return

    low, high = rule.get("low"), rule.get("high")
    exclusive = rule.get("exclusive", False)

    if low is not None:
        too_low = value <= low if exclusive else value < low
        if too_low:
            bound = f"> {low}" if exclusive else f">= {low}"
            result.errors.append(f"{key}: {value} must be {bound}")
            return

    if high is not None:
        too_high = value >= high if exclusive else value > high
        if too_high:
            bound = f"< {high}" if exclusive else f"<= {high}"
            result.errors.append(f"{key}: {value} must be {bound}")
            return

    warn_above = rule.get("warn_above")
    if warn_above is not None and value > warn_above:
        result.warnings.append(f"{key}: {value} is unusually high (typical is <= {warn_above})")


def cross_checks(bot, config, result):
    """Relationships between keys that per-key rules can't catch."""
    if bot == "weather":
        entry = config.get("entry_threshold")
        exit_ = config.get("exit_threshold")
        if isinstance(entry, (int, float)) and isinstance(exit_, (int, float)):
            if not isinstance(entry, bool) and not isinstance(exit_, bool) and exit_ <= entry:
                result.warnings.append(
                    f"exit_threshold ({exit_}) <= entry_threshold ({entry}): "
                    "positions would exit at or below their entry price"
                )

        locations = config.get("locations")
        if isinstance(locations, str) and locations.strip():
            names = [part.strip() for part in locations.split(",")]
            if any(not name for name in names):
                result.errors.append(f"locations: empty entry in {locations!r}")
            elif len(set(names)) != len(names):
                result.warnings.append(f"locations: duplicate entries in {locations!r}")

    if bot == "fastloop":
        window = config.get("window")
        min_time = config.get("min_time_remaining")
        if isinstance(window, str) and isinstance(min_time, (int, float)):
            window_seconds = {"5m": 300, "15m": 900}.get(window)
            if window_seconds and min_time >= window_seconds:
                result.errors.append(
                    f"min_time_remaining ({min_time}s) >= the whole {window} window "
                    f"({window_seconds}s): every market is skipped as too close to expiry"
                )


def validate_bot(bot):
    """Validate one bot's config.json against its trader's schema."""
    path = CONFIG_PATHS[bot]
    result = Result(bot, path)

    try:
        module = load_trader_module(bot)
    except (ImportError, FileNotFoundError) as exc:
        result.errors.append(str(exc))
        return result

    schema = getattr(module, "CONFIG_SCHEMA", None)
    if not schema:
        result.errors.append(f"{display_path(module.__file__)} defines no CONFIG_SCHEMA")
        return result

    if not os.path.isfile(path):
        result.notes.append("no config.json - the trader will use its built-in defaults")
        return result

    try:
        with open(path) as handle:
            config = json.load(handle)
    except json.JSONDecodeError as exc:
        result.errors.append(
            f"invalid JSON at line {exc.lineno} col {exc.colno}: {exc.msg}. "
            "The trader silently ignores this file and uses defaults instead."
        )
        return result
    except OSError as exc:
        result.errors.append(f"cannot read: {exc}")
        return result

    if not isinstance(config, dict):
        result.errors.append(f"expected a JSON object, got {type(config).__name__}")
        return result

    rules = RULES.get(bot, {})
    for key, value in config.items():
        spec = schema.get(key)
        if spec is None:
            result.warnings.append(f"{key}: not in CONFIG_SCHEMA - the trader ignores it")
            continue
        if check_type(key, value, spec.get("type", str), result) and key in rules:
            check_range(key, value, rules[key], module, result)

    for key in schema:
        if key not in config:
            result.notes.append(f"{key}: not set, defaults to {schema[key].get('default')!r}")

    cross_checks(bot, config, result)
    return result


def format_result(result):
    lines = ["=" * 50, f"{result.bot}  ({result.config_display})", "=" * 50]
    for error in result.errors:
        lines.append(f"  FAIL  {error}")
    for warning in result.warnings:
        lines.append(f"  WARN  {warning}")
    for note in result.notes:
        lines.append(f"  note  {note}")
    if result.ok and not result.warnings:
        lines.append("  OK    all values valid")
    lines.append("=" * 50)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Validate trader config.json files")
    parser.add_argument("--bot", choices=sorted(CONFIG_PATHS), action="append",
                        help="Only validate this bot (repeatable; default: all)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures")
    args = parser.parse_args()

    bots = args.bot or sorted(CONFIG_PATHS)
    results = [validate_bot(bot) for bot in bots]

    if args.json:
        print(json.dumps([result.as_dict() for result in results], indent=2))
    else:
        for result in results:
            print(format_result(result))
            print()
        failed = sum(len(r.errors) for r in results)
        warned = sum(len(r.warnings) for r in results)
        print(f"{len(results)} config(s) checked: {failed} error(s), {warned} warning(s)")

    if any(not result.ok for result in results):
        sys.exit(1)
    if args.strict and any(result.warnings for result in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
