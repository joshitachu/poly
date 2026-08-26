#!/usr/bin/env python3
"""
Log Summarizer

Aggregates the run logs written by run.sh (logs/weather.log, logs/fastloop.log)
into a single report: how many times each bot ran, how many trades it took, and
why it declined to trade the rest of the time.

Both traders are quiet by design in cron mode, so the logs are mostly a long
tail of "no trade" lines. This rolls that tail up so you can see whether a bot
is actually finding edges or just idling against a badly tuned threshold.

Usage:
    python3 scripts/summarize_logs.py
    python3 scripts/summarize_logs.py --since 24h
    python3 scripts/summarize_logs.py --json
    python3 scripts/summarize_logs.py --log logs/fastloop.log
"""

import os
import re
import sys
import json
import argparse
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(line_buffering=True)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOGS = ["logs/weather.log", "logs/fastloop.log"]

# run.sh writes one of these before each invocation: "--- 2026-08-26 14:03:01 UTC ---"
RUN_HEADER = re.compile(r"^-{3}\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+UTC\s+-{3}$")

# fastloop_trader.py: "Action: DRY RUN" / "TRADED" / "FAILED"
FASTLOOP_ACTION = re.compile(r"^\s*Action:\s*(DRY RUN|TRADED|FAILED)\s*$")
# fastloop_trader.py: "Summary: No trade (momentum too weak: 0.123%)"
FASTLOOP_NO_TRADE = re.compile(r"Summary:\s*(No trade|No markets|No tradeable)[^(]*(?:\((.*)\))?")
# fastloop_trader.py: "Signal: UP 0.123% | YES $0.512"
FASTLOOP_SIGNAL = re.compile(r"^\s*Signal:\s*(\w+)\s+(-?[\d.]+)%")

# weather_trader.py summary block: "  Trades executed:     2"
WEATHER_FIELD = re.compile(r"^\s{2}(Events scanned|Entry opportunities|Exit opportunities|Trades executed):\s*(\d+)\s*$")

ERROR_MARKERS = ("Error", "error:", "Trade failed", "Traceback", "❌")


def display_path(path):
    """Repo-relative for logs inside the repo, absolute for anything else."""
    absolute = os.path.abspath(path)
    if absolute.startswith(REPO_ROOT + os.sep):
        return os.path.relpath(absolute, REPO_ROOT)
    return absolute


def parse_since(spec):
    """Turn '24h' / '7d' / '90m' into a UTC cutoff datetime."""
    if not spec:
        return None
    match = re.fullmatch(r"(\d+)([mhd])", spec.strip().lower())
    if not match:
        raise ValueError(f"Bad --since value {spec!r} (expected forms: 90m, 24h, 7d)")
    amount, unit = int(match.group(1)), match.group(2)
    delta = {"m": timedelta(minutes=amount),
             "h": timedelta(hours=amount),
             "d": timedelta(days=amount)}[unit]
    return datetime.now(timezone.utc) - delta


def split_runs(lines):
    """Yield (timestamp_or_None, [lines]) for each run.sh invocation in a log."""
    current_ts, buffer = None, []
    for line in lines:
        header = RUN_HEADER.match(line.rstrip())
        if header:
            if buffer or current_ts:
                yield current_ts, buffer
            stamp = datetime.strptime(header.group(1), "%Y-%m-%d %H:%M:%S")
            current_ts, buffer = stamp.replace(tzinfo=timezone.utc), []
        else:
            buffer.append(line)
    if buffer or current_ts:
        yield current_ts, buffer


def normalize_reason(reason):
    """'momentum too weak: 0.123%' -> 'momentum too weak' so reasons group."""
    if not reason:
        return "unspecified"
    return reason.split(":")[0].strip().lower()


def summarize_run(lines):
    """Extract the interesting facts from one run's output."""
    run = {"trades": 0, "action": None, "reason": None, "errors": [], "signal_pct": None}
    for line in lines:
        stripped = line.rstrip()

        action = FASTLOOP_ACTION.match(stripped)
        if action:
            run["action"] = action.group(1)
            if action.group(1) == "TRADED":
                run["trades"] += 1
            continue

        no_trade = FASTLOOP_NO_TRADE.search(stripped)
        if no_trade:
            run["reason"] = normalize_reason(no_trade.group(2) or no_trade.group(1))
            continue

        signal = FASTLOOP_SIGNAL.match(stripped)
        if signal:
            run["signal_pct"] = abs(float(signal.group(2)))
            continue

        field = WEATHER_FIELD.match(stripped)
        if field:
            if field.group(1) == "Trades executed":
                run["trades"] += int(field.group(2))
            run.setdefault("weather", {})[field.group(1)] = int(field.group(2))
            continue

        if any(marker in stripped for marker in ERROR_MARKERS):
            run["errors"].append(stripped.strip())

    return run


def summarize_log(path, cutoff):
    """Aggregate one log file into a report dict."""
    with open(path, "r", errors="replace") as handle:
        lines = handle.readlines()

    report = {
        "log": display_path(path),
        "runs": 0,
        "runs_skipped_by_filter": 0,
        "trades": 0,
        "dry_runs": 0,
        "failures": 0,
        "reasons": {},
        "errors": [],
        "first_run": None,
        "last_run": None,
        "signals": [],
        "events_scanned": 0,
    }

    for timestamp, body in split_runs(lines):
        if cutoff and timestamp and timestamp < cutoff:
            report["runs_skipped_by_filter"] += 1
            continue

        run = summarize_run(body)
        report["runs"] += 1
        report["trades"] += run["trades"]
        if run["action"] == "DRY RUN":
            report["dry_runs"] += 1
        elif run["action"] == "FAILED":
            report["failures"] += 1
        if run["reason"]:
            report["reasons"][run["reason"]] = report["reasons"].get(run["reason"], 0) + 1
        if run["signal_pct"] is not None:
            report["signals"].append(run["signal_pct"])
        if "weather" in run:
            report["events_scanned"] += run["weather"].get("Events scanned", 0)
        report["errors"].extend(run["errors"])

        if timestamp:
            if report["first_run"] is None or timestamp < report["first_run"]:
                report["first_run"] = timestamp
            if report["last_run"] is None or timestamp > report["last_run"]:
                report["last_run"] = timestamp

    return report


def format_report(report):
    """Render one log's report as plain text."""
    out = []
    out.append("=" * 50)
    out.append(report["log"])
    out.append("=" * 50)

    if report["runs"] == 0:
        out.append("  No runs in range")
        if report["runs_skipped_by_filter"]:
            out.append(f"  ({report['runs_skipped_by_filter']} older runs excluded by --since)")
        out.append("=" * 50)
        return "\n".join(out)

    span = ""
    if report["first_run"] and report["last_run"]:
        span = (f"{report['first_run']:%Y-%m-%d %H:%M} -> "
                f"{report['last_run']:%Y-%m-%d %H:%M} UTC")

    out.append(f"  Runs:            {report['runs']}")
    if span:
        out.append(f"  Window:          {span}")
    out.append(f"  Trades executed: {report['trades']}")
    if report["dry_runs"]:
        out.append(f"  Dry runs:        {report['dry_runs']}")
    if report["failures"]:
        out.append(f"  Failed trades:   {report['failures']}")
    if report["events_scanned"]:
        out.append(f"  Events scanned:  {report['events_scanned']}")

    if report["signals"]:
        signals = report["signals"]
        out.append(f"  Momentum |%|:    avg {sum(signals) / len(signals):.3f}, "
                   f"max {max(signals):.3f}")

    if report["reasons"]:
        out.append("\n  No-trade reasons:")
        ranked = sorted(report["reasons"].items(), key=lambda kv: -kv[1])
        width = max(len(reason) for reason, _ in ranked)
        for reason, count in ranked:
            share = count / report["runs"]
            out.append(f"      {reason.ljust(width)}  {count:>4}  ({share:.0%})")

    if report["errors"]:
        unique = sorted(set(report["errors"]))
        out.append(f"\n  Errors: {len(report['errors'])} ({len(unique)} distinct)")
        for error in unique[:5]:
            out.append(f"      {error[:90]}")
        if len(unique) > 5:
            out.append(f"      ... and {len(unique) - 5} more")

    if report["runs_skipped_by_filter"]:
        out.append(f"\n  ({report['runs_skipped_by_filter']} older runs excluded by --since)")

    out.append("=" * 50)
    return "\n".join(out)


def jsonable(report):
    """Copy of a report with datetimes as ISO strings, for --json."""
    clone = dict(report)
    for key in ("first_run", "last_run"):
        clone[key] = report[key].isoformat() if report[key] else None
    clone["errors"] = sorted(set(report["errors"]))
    clone.pop("signals", None)
    return clone


def main():
    parser = argparse.ArgumentParser(description="Summarize weather/fastloop run logs")
    parser.add_argument("--log", action="append", metavar="PATH",
                        help="Log file to read (repeatable; default: logs/weather.log, logs/fastloop.log)")
    parser.add_argument("--since", metavar="SPEC",
                        help="Only count runs newer than this (e.g. 90m, 24h, 7d)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = parser.parse_args()

    try:
        cutoff = parse_since(args.since)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(2)

    candidates = args.log or [os.path.join(REPO_ROOT, name) for name in DEFAULT_LOGS]
    existing = [path for path in candidates if os.path.isfile(path)]

    missing = [path for path in candidates if path not in existing]
    if not existing:
        print("No log files found. Looked for:")
        for path in missing:
            print(f"  {path}")
        print("\nLogs appear once the bots run via cron — see the README.")
        sys.exit(1)

    reports = [summarize_log(path, cutoff) for path in existing]

    if args.json:
        print(json.dumps([jsonable(report) for report in reports], indent=2))
        return

    for report in reports:
        print(format_report(report))
        print()

    total_runs = sum(report["runs"] for report in reports)
    total_trades = sum(report["trades"] for report in reports)
    if len(reports) > 1:
        print(f"Across {len(reports)} logs: {total_runs} runs, {total_trades} trades")
    if missing:
        print(f"Skipped {len(missing)} missing log(s): "
              f"{', '.join(display_path(p) for p in missing)}")


if __name__ == "__main__":
    main()
