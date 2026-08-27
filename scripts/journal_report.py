#!/usr/bin/env python3
"""
Journal Report

Aggregates logs/trades.jsonl - the file tradejournal.log_trade() writes - into a
report per bot: how many trades, over what period, at what confidence, and the
signal readings that justified them.

This answers a different question from `./run.sh summary`. That reads the run
logs and tells you what the bots *did* on each invocation, including all the
times they declined to trade. This reads the journal and tells you what the
executed trades *were*, with the thesis and confidence attached.

Usage:
    python3 scripts/journal_report.py
    python3 scripts/journal_report.py --since 7d
    python3 scripts/journal_report.py --source sdk:weather
    python3 scripts/journal_report.py --recent 5
    python3 scripts/journal_report.py --json
"""

import os
import re
import sys
import json
import argparse
from datetime import datetime, timezone, timedelta

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from tradejournal import journal_path, read_trades  # noqa: E402

sys.stdout.reconfigure(line_buffering=True)

# Signal fields worth averaging when a bot records them
NUMERIC_EXTRAS = ("momentum_pct", "volume_ratio", "forecast_temp")


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


def record_time(record):
    """Parse a record's timestamp, or None if it is missing/unreadable."""
    raw = record.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # Treat a naive timestamp as UTC rather than discarding the record
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def mean(values):
    return sum(values) / len(values) if values else None


def summarize(records):
    """Group records by source into per-bot stats."""
    groups = {}

    for record in records:
        source = record.get("source") or "unknown"
        group = groups.setdefault(source, {
            "source": source,
            "trades": 0,
            "buys": 0,
            "sells": 0,
            "confidences": [],
            "first": None,
            "last": None,
            "extras": {},
            "missing_thesis": 0,
        })

        group["trades"] += 1
        action = record.get("action", "buy")
        if action == "sell":
            group["sells"] += 1
        else:
            group["buys"] += 1

        confidence = record.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            group["confidences"].append(float(confidence))

        if not record.get("thesis"):
            group["missing_thesis"] += 1

        stamp = record_time(record)
        if stamp:
            if group["first"] is None or stamp < group["first"]:
                group["first"] = stamp
            if group["last"] is None or stamp > group["last"]:
                group["last"] = stamp

        extra = record.get("extra")
        if isinstance(extra, dict):
            for key, value in extra.items():
                bucket = group["extras"].setdefault(key, {"count": 0, "numeric": [], "values": {}})
                bucket["count"] += 1
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    bucket["numeric"].append(float(value))
                else:
                    text = str(value)
                    bucket["values"][text] = bucket["values"].get(text, 0) + 1

    return [groups[key] for key in sorted(groups)]


def format_group(group):
    out = []
    out.append("=" * 62)
    out.append(f"{group['source']}  -  {group['trades']} trade(s)")
    out.append("=" * 62)

    if group["first"] and group["last"]:
        out.append(f"  Window:      {group['first']:%Y-%m-%d %H:%M} -> "
                   f"{group['last']:%Y-%m-%d %H:%M} UTC")

    out.append(f"  Buys/Sells:  {group['buys']} / {group['sells']}")

    average = mean(group["confidences"])
    if average is not None:
        out.append(f"  Confidence:  avg {average:.2f}, "
                   f"min {min(group['confidences']):.2f}, "
                   f"max {max(group['confidences']):.2f} "
                   f"({len(group['confidences'])} of {group['trades']} recorded)")
    else:
        out.append("  Confidence:  none recorded")

    if group["missing_thesis"]:
        out.append(f"  No thesis:   {group['missing_thesis']} record(s)")

    if group["extras"]:
        out.append("\n  Signal fields:")
        for key in sorted(group["extras"]):
            bucket = group["extras"][key]
            if bucket["numeric"]:
                out.append(f"      {key}: avg {mean(bucket['numeric']):+.3f} "
                           f"(min {min(bucket['numeric']):+.3f}, "
                           f"max {max(bucket['numeric']):+.3f})")
            else:
                ranked = sorted(bucket["values"].items(), key=lambda kv: -kv[1])[:4]
                rendered = ", ".join(f"{value} x{count}" for value, count in ranked)
                out.append(f"      {key}: {rendered}")

    out.append("=" * 62)
    return "\n".join(out)


def jsonable(group):
    clone = {
        "source": group["source"],
        "trades": group["trades"],
        "buys": group["buys"],
        "sells": group["sells"],
        "confidence_avg": mean(group["confidences"]),
        "confidence_samples": len(group["confidences"]),
        "missing_thesis": group["missing_thesis"],
        "first": group["first"].isoformat() if group["first"] else None,
        "last": group["last"].isoformat() if group["last"] else None,
        "extras": {},
    }
    for key, bucket in group["extras"].items():
        clone["extras"][key] = {
            "count": bucket["count"],
            "avg": mean(bucket["numeric"]),
            "values": bucket["values"] or None,
        }
    return clone


def main():
    parser = argparse.ArgumentParser(description="Report on recorded trades")
    parser.add_argument("--since", metavar="SPEC", help="Only trades newer than this (90m, 24h, 7d)")
    parser.add_argument("--source", action="append", metavar="NAME",
                        help="Only this source, e.g. sdk:weather (repeatable)")
    parser.add_argument("--recent", type=int, metavar="N", help="Also print the N latest theses")
    parser.add_argument("--path", metavar="PATH", help="Read a journal other than the default")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = parser.parse_args()

    try:
        cutoff = parse_since(args.since)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(2)

    path = args.path or journal_path()
    records, bad_lines = read_trades(path)

    if not records:
        if args.json:
            print(json.dumps({"journal": path, "trades": 0, "sources": []}, indent=2))
            return
        print(f"No trades recorded in {path}")
        print("\nThe traders journal live trades only - the weather bot skips")
        print("simulated ones, so a paper-mode-only history stays empty here.")
        return

    total_before_filter = len(records)

    if cutoff:
        records = [r for r in records if (record_time(r) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
    if args.source:
        wanted = set(args.source)
        records = [r for r in records if r.get("source") in wanted]

    groups = summarize(records)

    if args.json:
        print(json.dumps({
            "journal": path,
            "trades": len(records),
            "trades_before_filters": total_before_filter,
            "bad_lines": bad_lines,
            "sources": [jsonable(group) for group in groups],
        }, indent=2))
        return

    if not records:
        print(f"No trades in {path} match the given filters "
              f"({total_before_filter} record(s) total)")
        return

    for group in groups:
        print(format_group(group))
        print()

    if args.recent:
        print(f"Latest {args.recent} thesis line(s):")
        ordered = sorted(records, key=lambda r: record_time(r) or datetime.min.replace(tzinfo=timezone.utc))
        for record in ordered[-args.recent:]:
            stamp = record_time(record)
            when = f"{stamp:%Y-%m-%d %H:%M}" if stamp else "unknown time"
            thesis = record.get("thesis") or "(no thesis recorded)"
            print(f"  {when}  [{record.get('source', '?')}]  {thesis}")
        print()

    print(f"{len(records)} of {total_before_filter} record(s) shown from {path}")
    if bad_lines:
        print(f"Warning: {bad_lines} unparseable line(s) skipped")


if __name__ == "__main__":
    main()
