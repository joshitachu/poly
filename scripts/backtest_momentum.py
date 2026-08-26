#!/usr/bin/env python3
"""
Momentum Backtest

Replays the fastloop momentum rule over historical Binance 1m candles and
reports how often the signal was followed by continued movement in the same
direction. Sweeps a range of min_momentum_pct values so you can see which
threshold, if any, actually separates signal from noise before committing it to
config.json.

The momentum calculation mirrors get_binance_momentum() in fastloop_trader.py
exactly: price_then is the open of the oldest candle in the lookback window,
price_now is the close of the newest, and volume_ratio is the newest candle's
volume over the window average.

IMPORTANT - what this does not model: the trader only buys when the Polymarket
price diverges from 50c by entry_threshold, and historical Polymarket prices are
not available here. So this measures the *hypothesis* the strategy rests on
("momentum continues over the next window"), not the strategy's P&L. Compare
every hit rate against the printed base rate; a hit rate near the base rate
means the signal carries no information, however profitable it looks.

Usage:
    python3 scripts/backtest_momentum.py
    python3 scripts/backtest_momentum.py --days 7 --asset ETH
    python3 scripts/backtest_momentum.py --thresholds 0.1,0.25,0.5,1.0
    python3 scripts/backtest_momentum.py --json
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import load_trader_module  # noqa: E402

sys.stdout.reconfigure(line_buffering=True)

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
MAX_LIMIT = 1000          # Binance's per-request cap
REQUEST_PAUSE = 0.2       # be polite to a public, unauthenticated endpoint
DEFAULT_THRESHOLDS = [0.05, 0.1, 0.25, 0.5, 0.75, 1.0]
WINDOW_MINUTES = {"5m": 5, "15m": 15}

# Volume gate from fastloop_trader.py: skip when volume_ratio < 0.5
LOW_VOLUME_RATIO = 0.5


def fetch_klines(symbol, minutes, verbose=True):
    """Fetch `minutes` of 1m candles ending now, paging backwards."""
    candles = []
    end_time = int(time.time() * 1000)
    remaining = minutes

    while remaining > 0:
        limit = min(MAX_LIMIT, remaining)
        url = (f"{BINANCE_KLINES}?symbol={symbol}&interval=1m"
               f"&limit={limit}&endTime={end_time}")
        request = Request(url, headers={"User-Agent": "poly-backtest/1.0"})
        try:
            with urlopen(request, timeout=30) as response:
                batch = json.loads(response.read().decode())
        except HTTPError as exc:
            body = exc.read().decode() if exc.fp else ""
            print(f"Binance API error {exc.code}: {body[:200]}")
            sys.exit(1)
        except (URLError, TimeoutError) as exc:
            print(f"Connection error reaching Binance: {exc}")
            sys.exit(1)
        except json.JSONDecodeError:
            print("Binance returned a non-JSON response")
            sys.exit(1)

        if not batch:
            break

        candles = batch + candles
        remaining -= len(batch)
        end_time = int(batch[0][0]) - 1   # step back past the oldest we just got

        if verbose:
            print(f"  fetched {len(candles)} candles...", end="\r")
        if len(batch) < limit:
            break                        # history exhausted
        time.sleep(REQUEST_PAUSE)

    if verbose and candles:
        print(f"  fetched {len(candles)} candles      ")
    return candles


def load_candles(path):
    """Read a saved kline array (same shape Binance returns)."""
    try:
        with open(path) as handle:
            candles = json.load(handle)
    except OSError as exc:
        print(f"Error: cannot read {path}: {exc}")
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"Error: {path} is not valid JSON: {exc}")
        sys.exit(1)

    if not isinstance(candles, list) or not candles:
        print(f"Error: {path} does not contain a non-empty array of klines")
        sys.exit(1)
    if not all(isinstance(row, list) and len(row) >= 6 for row in candles):
        print(f"Error: {path} rows must be klines with at least 6 fields")
        sys.exit(1)
    return candles


def momentum_at(window):
    """Momentum over one lookback window, matching get_binance_momentum()."""
    price_then = float(window[0][1])     # open of oldest candle
    price_now = float(window[-1][4])     # close of newest candle
    if price_then == 0:
        return None

    momentum_pct = ((price_now - price_then) / price_then) * 100
    volumes = [float(candle[5]) for candle in window]
    avg_volume = sum(volumes) / len(volumes)
    volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else 1.0

    return {
        "momentum_pct": momentum_pct,
        "direction": "up" if momentum_pct > 0 else "down",
        "price_now": price_now,
        "volume_ratio": volume_ratio,
    }


def base_rate(candles, horizon):
    """Unconditional P(price rises over `horizon`) - the benchmark to beat."""
    ups = flats = total = 0
    for index in range(len(candles) - horizon):
        entry = float(candles[index][4])
        exit_price = float(candles[index + horizon][4])
        total += 1
        if exit_price > entry:
            ups += 1
        elif exit_price == entry:
            flats += 1
    if not total:
        return None
    return {"samples": total, "up_rate": ups / total, "flat_rate": flats / total}


def evaluate(candles, threshold, lookback, horizon, volume_gate):
    """Count signals at `threshold` and how many continued in-direction."""
    stats = {
        "threshold": threshold,
        "signals": 0,
        "wins": 0,
        "losses": 0,
        "flat": 0,
        "filtered_low_volume": 0,
        "moves": [],
    }

    last_index = len(candles) - horizon
    for index in range(lookback, last_index):
        window = candles[index - lookback:index]
        signal = momentum_at(window)
        if signal is None or abs(signal["momentum_pct"]) < threshold:
            continue

        if volume_gate and signal["volume_ratio"] < LOW_VOLUME_RATIO:
            stats["filtered_low_volume"] += 1
            continue

        entry = signal["price_now"]
        exit_price = float(candles[index - 1 + horizon][4])
        if entry == 0:
            continue

        move_pct = ((exit_price - entry) / entry) * 100
        signed = move_pct if signal["direction"] == "up" else -move_pct

        stats["signals"] += 1
        stats["moves"].append(signed)
        if exit_price == entry:
            stats["flat"] += 1
        elif signed > 0:
            stats["wins"] += 1
        else:
            stats["losses"] += 1

    decided = stats["wins"] + stats["losses"]
    stats["hit_rate"] = stats["wins"] / decided if decided else None
    stats["avg_move_pct"] = sum(stats["moves"]) / len(stats["moves"]) if stats["moves"] else None
    stats.pop("moves")
    return stats


def parse_thresholds(spec):
    if not spec:
        return DEFAULT_THRESHOLDS
    values = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = float(chunk)
        except ValueError:
            raise ValueError(f"Bad threshold {chunk!r} - expected a number like 0.25")
        if value < 0:
            raise ValueError(f"Threshold {value} must be >= 0")
        values.append(value)
    if not values:
        raise ValueError("No thresholds given")
    return sorted(set(values))


def format_report(meta, base, rows, configured):
    out = []
    out.append("=" * 68)
    out.append(f"MOMENTUM BACKTEST  {meta['symbol']}  {meta['days']}d "
               f"({meta['candles']} 1m candles)")
    out.append("=" * 68)
    out.append(f"  Window:    {meta['first']} -> {meta['last']} UTC")
    out.append(f"  Lookback:  {meta['lookback']}m   Horizon: {meta['horizon']}m "
               f"({meta['window']} market)")
    out.append(f"  Vol gate:  {'on (skip < 0.5x avg)' if meta['volume_gate'] else 'off'}")

    if base:
        out.append(f"  Base rate: {base['up_rate']:.1%} of all {meta['horizon']}m periods "
                   f"rose ({base['samples']} samples)")
        out.append("             ^ beat this, or the signal adds nothing")

    out.append("")
    out.append("  min_momentum_pct   signals   hit rate   vs base   avg move   vol-filtered")
    out.append("  " + "-" * 64)

    benchmark = base["up_rate"] if base else None
    for row in rows:
        marker = " <- config" if configured is not None and abs(row["threshold"] - configured) < 1e-9 else ""
        if row["hit_rate"] is None:
            out.append(f"  {row['threshold']:>10.2f}%      {row['signals']:>6}"
                       f"        n/a        n/a        n/a   {row['filtered_low_volume']:>10}{marker}")
            continue
        edge = f"{(row['hit_rate'] - benchmark) * 100:+.1f}pp" if benchmark is not None else "n/a"
        out.append(f"  {row['threshold']:>10.2f}%      {row['signals']:>6}"
                   f"     {row['hit_rate']:>6.1%}   {edge:>8}"
                   f"   {row['avg_move_pct']:>+7.3f}%   {row['filtered_low_volume']:>10}{marker}")

    out.append("")
    out.append("  Reminder: this ignores Polymarket pricing and fees, so it tests")
    out.append("  whether momentum persists - not whether the bot makes money.")
    out.append("=" * 68)
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="Backtest the fastloop momentum signal")
    parser.add_argument("--days", type=float, default=3.0,
                        help="Days of 1m history to pull (default: 3)")
    parser.add_argument("--asset", help="Asset symbol key, e.g. BTC/ETH/SOL (default: from config.json)")
    parser.add_argument("--lookback", type=int,
                        help="Momentum lookback in minutes (default: from config.json)")
    parser.add_argument("--window", choices=sorted(WINDOW_MINUTES),
                        help="Market window, sets the outcome horizon (default: from config.json)")
    parser.add_argument("--thresholds", help="Comma-separated min_momentum_pct values to sweep")
    parser.add_argument("--no-volume-gate", action="store_true",
                        help="Ignore the volume_ratio < 0.5 filter")
    parser.add_argument("--candles-file", metavar="PATH",
                        help="Replay a saved kline JSON array instead of calling Binance")
    parser.add_argument("--save-candles", metavar="PATH",
                        help="Write fetched klines here so later sweeps can skip the fetch")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = parser.parse_args()

    try:
        thresholds = parse_thresholds(args.thresholds)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(2)

    if args.days <= 0:
        print("Error: --days must be > 0")
        sys.exit(2)

    # Defaults come from the trader's own live config so the sweep is centred
    # on what the bot is actually running.
    try:
        fastloop = load_trader_module("fastloop")
        cfg = fastloop.cfg
        symbols = fastloop.ASSET_SYMBOLS
    except (ImportError, FileNotFoundError, AttributeError) as exc:
        print(f"Error: could not read fastloop config ({exc})")
        sys.exit(1)

    asset = (args.asset or cfg["asset"]).upper()
    if asset not in symbols:
        print(f"Error: unknown asset {asset!r} (known: {', '.join(sorted(symbols))})")
        sys.exit(2)

    lookback = args.lookback if args.lookback is not None else int(cfg["lookback_minutes"])
    if lookback < 2:
        print("Error: --lookback must be >= 2 (the trader needs 2+ candles)")
        sys.exit(2)

    window = args.window or cfg["window"]
    horizon = WINDOW_MINUTES.get(window)
    if horizon is None:
        print(f"Error: window {window!r} is not one of {', '.join(sorted(WINDOW_MINUTES))}")
        sys.exit(2)

    volume_gate = not args.no_volume_gate and bool(cfg["volume_confidence"])
    symbol = symbols[asset]
    minutes = int(args.days * 24 * 60)

    if args.candles_file:
        candles = load_candles(args.candles_file)
    else:
        if not args.json:
            print(f"Fetching {minutes} 1m candles for {symbol}...")
        candles = fetch_klines(symbol, minutes, verbose=not args.json)
        if args.save_candles:
            with open(args.save_candles, "w") as handle:
                json.dump(candles, handle)
            if not args.json:
                print(f"  saved {len(candles)} candles to {args.save_candles}")

    needed = lookback + horizon + 1
    if len(candles) < needed:
        print(f"Error: only {len(candles)} candles returned, need at least {needed}")
        sys.exit(1)

    meta = {
        "symbol": symbol,
        "asset": asset,
        "days": round(len(candles) / 1440, 2),
        "candles": len(candles),
        "lookback": lookback,
        "horizon": horizon,
        "window": window,
        "volume_gate": volume_gate,
        "first": datetime.fromtimestamp(candles[0][0] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "last": datetime.fromtimestamp(candles[-1][0] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"),
    }

    base = base_rate(candles, horizon)
    rows = [evaluate(candles, threshold, lookback, horizon, volume_gate)
            for threshold in thresholds]
    configured = float(cfg["min_momentum_pct"])

    if args.json:
        print(json.dumps({"meta": meta, "base_rate": base,
                          "configured_min_momentum_pct": configured,
                          "results": rows}, indent=2))
        return

    print()
    print(format_report(meta, base, rows, configured))


if __name__ == "__main__":
    main()
