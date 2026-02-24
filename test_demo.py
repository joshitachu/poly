#!/usr/bin/env python3
"""
Test Demo — Run both trading bots in verbose mode with annotated output.

This script walks you through exactly what each bot does, step by step,
so you can see the full decision flow before deploying to a VPS.

Usage:
    python test_demo.py              # Run both bots (dry run)
    python test_demo.py weather      # Run weather bot only
    python test_demo.py fastloop     # Run fastloop bot only
"""

import os
import sys
import subprocess
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env if it exists (so we don't need to source it manually)
env_path = os.path.join(SCRIPT_DIR, ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ[key] = value


def banner(text):
    width = 60
    print()
    print("=" * width)
    print(f"  {text}")
    print("=" * width)


def section(text):
    print(f"\n--- {text} ---\n")


def explain(text):
    for line in text.strip().splitlines():
        print(f"    {line}")
    print()


def run_bot(script_path, label):
    """Run a bot script and stream its output in real time."""
    banner(f"RUNNING: {label}")
    print(f"  Script: {script_path}")
    print(f"  Mode:   DRY RUN (no real trades)\n")

    try:
        result = subprocess.run(
            [sys.executable, script_path],
            cwd=SCRIPT_DIR,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)
        if result.returncode != 0:
            print(f"  Exit code: {result.returncode}")
    except subprocess.TimeoutExpired:
        print("  TIMEOUT: Bot took longer than 120 seconds")
    except FileNotFoundError:
        print(f"  ERROR: Script not found at {script_path}")


def demo_weather():
    banner("WEATHER BOT")
    explain("""
How it works:
  1. Ask NOAA: what's the forecast for NYC/Chicago/Miami?
  2. Ask Simmer: what weather markets exist on Polymarket?
  3. Compare: is the market price BELOW what NOAA says is likely?
  4. If yes -> BUY (paper trade)

Example:
  NOAA says: "NYC tomorrow = 42F"
  Polymarket has: "NYC 42-43F" bucket priced at $0.10
  Threshold: buy if price < $0.36
  $0.10 < $0.36 -> BUY! (NOAA thinks it's likely, market is cheap)
""")

    weather_script = os.path.join(SCRIPT_DIR, "weather-trader", "weather_trader.py")
    run_bot(weather_script, "Weather Trader (paper mode)")


def demo_fastloop():
    banner("BTC FASTLOOP BOT")
    explain("""
How it works:
  1. Ask Binance: is BTC going up or down right now?
  2. Ask Polymarket: any "BTC up/down in 5 min" markets?
  3. Compare: does the market price match what Binance shows?
  4. If mismatch -> BUY (paper trade)

Example:
  Binance says: "BTC up +0.8% in last 5 min"
  Polymarket has: "BTC up or down 5:30-5:35 ET?" YES at $0.48
  Momentum says UP, but market only $0.48 (should be higher)
  -> Divergence detected -> BUY YES
""")

    fastloop_script = os.path.join(SCRIPT_DIR, "fastloop_trader.py")
    run_bot(fastloop_script, "FastLoop BTC Trader (paper mode)")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"

    banner("POLYMARKET PAPER TRADING - TEST DEMO")
    print(f"  Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")

    api_key = os.environ.get("SIMMER_API_KEY", "")
    if not api_key:
        print("\n  WARNING: SIMMER_API_KEY not set!")
        print("  Create a .env file with: SIMMER_API_KEY=sk_live_...")
        print("  Or export it: export SIMMER_API_KEY=sk_live_...")
        sys.exit(1)
    print(f"  API Key: {api_key[:12]}...{api_key[-4:]}")
    print(f"  Mode:    DRY RUN (paper trading only)")

    if mode in ("weather", "both"):
        demo_weather()

    if mode in ("fastloop", "both"):
        demo_fastloop()

    banner("DONE")
    print("  Both bots ran in paper mode. No real money was used.")
    print("  To deploy on VPS, see README.md for cron setup.\n")


if __name__ == "__main__":
    main()
