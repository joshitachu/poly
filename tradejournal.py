#!/usr/bin/env python3
"""
Trade Journal

Both traders already try to import log_trade() from here:

    try:
        from tradejournal import log_trade
        JOURNAL_AVAILABLE = True
    except ImportError:
        ...
        def log_trade(*args, **kwargs):
            pass

Until now that import failed and the no-op won, so every thesis, confidence
score and signal reading passed to log_trade() was thrown away. This module
implements it: one JSON object per line appended to logs/trades.jsonl, which
scripts/journal_report.py then aggregates.

Design rule: log_trade() must never raise. It is called immediately after a
trade has already executed, so an exception here would turn a successful trade
into a crashed run with no record of what happened. Every failure path warns on
stderr and returns False instead.

Usage as a module:
    from tradejournal import log_trade
    log_trade(trade_id="abc", source="sdk:weather", thesis="...", confidence=0.7)

Usage from the CLI (inspect what has been recorded):
    python3 tradejournal.py --tail 10
    python3 tradejournal.py --count
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Override with SIMMER_JOURNAL_PATH to keep separate journals per environment
DEFAULT_JOURNAL = os.path.join(REPO_ROOT, "logs", "trades.jsonl")

# Fields the traders pass that belong at the top level of every record. Anything
# else they pass (asset, momentum_pct, location, metric, ...) is kept verbatim
# under "extra" so this module never has to change when a trader adds a field.
CORE_FIELDS = ("trade_id", "source", "thesis", "confidence", "action")


def journal_path():
    """Where the journal lives. Env override wins."""
    return os.environ.get("SIMMER_JOURNAL_PATH") or DEFAULT_JOURNAL


def _warn(message):
    print(f"  [journal] {message}", file=sys.stderr)


def build_record(*args, **fields):
    """Normalize one log_trade() call into the record we persist.

    Positional arguments are not used by any current call site, but the no-op
    fallback accepted them, so they are preserved rather than dropped.
    """
    record = {"ts": datetime.now(timezone.utc).isoformat()}

    for key in CORE_FIELDS:
        if key in fields:
            record[key] = fields.pop(key)

    # Entries have no explicit action; exits pass action="sell"
    record.setdefault("action", "buy")

    if args:
        record["args"] = [repr(arg) for arg in args]
    if fields:
        record["extra"] = fields

    return record


def log_trade(*args, **fields):
    """Append one trade to the journal. Returns True on success.

    Never raises - see the module docstring.
    """
    try:
        record = build_record(*args, **fields)
        path = journal_path()

        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        # One write of a single short line, opened O_APPEND: concurrent runs of
        # the two bots interleave cleanly without a lock file.
        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
        return True
    except Exception as exc:  # noqa: BLE001 - a journal failure must not kill a trade
        _warn(f"could not record trade: {exc}")
        return False


def read_trades(path=None, skip_bad=True):
    """Read the journal. Returns (records, bad_line_count).

    Tolerates truncated or corrupt lines - a half-written line from a killed
    process should not make the whole journal unreadable.
    """
    path = path or journal_path()
    records, bad = [], 0

    if not os.path.isfile(path):
        return records, bad

    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                if not skip_bad:
                    raise
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                bad += 1

    return records, bad


def main():
    parser = argparse.ArgumentParser(description="Inspect the trade journal")
    parser.add_argument("--tail", type=int, metavar="N", help="Show the last N records")
    parser.add_argument("--count", action="store_true", help="Print the record count only")
    parser.add_argument("--path", action="store_true", help="Print the journal path only")
    args = parser.parse_args()

    if args.path:
        print(journal_path())
        return

    records, bad = read_trades()

    if args.count:
        print(len(records))
        return

    if not records:
        print(f"No trades recorded yet at {journal_path()}")
        print("The traders only journal live trades - weather skips simulated ones.")
        return

    selection = records[-args.tail:] if args.tail else records
    for record in selection:
        print(json.dumps(record, ensure_ascii=False))

    if bad:
        _warn(f"{bad} unparseable line(s) skipped")


if __name__ == "__main__":
    main()
