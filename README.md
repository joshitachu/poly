# Polymarket Paper Trading Bots

Two automated paper-trading bots for Polymarket, powered by [Simmer](https://simmer.markets). The **weather bot** trades temperature markets using NOAA forecasts, and the **fastloop bot** trades BTC 5-minute markets using Binance price momentum.

## Quick Start

Requires Python 3.9+.

```bash
# 1. Install the SDK
pip install -r requirements.txt

# 2. Create your .env file
cp .env.example .env
# Edit .env and add your API key from https://simmer.markets/dashboard

# 3. Run the demo to see both bots in action
python3 test_demo.py
```

## Running Individual Bots

```bash
# Weather bot (dry run)
./run.sh weather

# Fastloop BTC bot (dry run)
./run.sh fastloop

# Check account balance & positions
./run.sh status

# Summarize past runs from logs/
./run.sh summary
```

`run.sh` calls `python3` by default. To use a virtualenv or another interpreter,
set `PYTHON`:

```bash
PYTHON=./venv/bin/python ./run.sh fastloop
```

## VPS Deployment

```bash
# 1. Clone the repo
git clone https://github.com/joshitachu/poly.git
cd poly

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set up your API key
cp .env.example .env
nano .env  # paste your SIMMER_API_KEY

# 4. Check the box is ready (exits non-zero if not)
./run.sh doctor

# 5. Test it works
python3 test_demo.py

# 6. Add cron jobs
crontab -e
```

Add these lines to your crontab:

```cron
# Weather bot every 30 minutes
*/30 * * * * /path/to/poly/run.sh weather >> /path/to/poly/logs/weather.log 2>&1

# Fastloop bot every 5 minutes
*/5  * * * * /path/to/poly/run.sh fastloop >> /path/to/poly/logs/fastloop.log 2>&1
```

## Diagnostics

None of these need an API key or place a trade.

```bash
./run.sh doctor              # preflight: interpreter, SDK, key, configs, logs, APIs
./run.sh doctor --offline    # skip the network probes
./run.sh validate            # check both config.json files
./run.sh test                # run the unit tests
```

Run `./run.sh doctor` after the VPS steps below and before adding cron entries.
Cron failures are silent: a bot that dies on startup every five minutes looks
exactly like a bot that found no trades. `doctor` exits non-zero if anything is
actually broken, so it works as a deploy gate.

`./run.sh validate` is worth running after any config edit. The traders load
config with a `try/except` that swallows JSON errors and falls back to built-in
defaults — so a stray comma means the bot keeps trading with parameters that
don't match the file you just edited, and nothing in the logs mentions it. The
validator also catches values that are legal JSON but unusable, e.g.
`lookback_minutes: 1` (the momentum call needs two candles) or a
`min_time_remaining` longer than the whole market window.

## Tuning the Momentum Threshold

`min_momentum_pct` is the fastloop bot's main dial. `./run.sh backtest` replays
the same momentum calculation the trader uses over historical Binance candles
and sweeps a range of thresholds:

```bash
./run.sh backtest                          # 3 days, settings from config.json
./run.sh backtest --days 7 --asset ETH
./run.sh backtest --thresholds 0.1,0.25,0.5
./run.sh backtest --save-candles /tmp/btc.json   # then --candles-file to re-sweep offline
```

Read the output against the printed base rate, not on its own. The base rate is
how often price rose over the same horizon with no signal at all; a hit rate
near it means the threshold carries no information no matter how high it looks.

This is deliberately not a P&L backtest — historical Polymarket prices aren't
available here, so it can't model the `entry_threshold` divergence check or
fees. It tests the hypothesis the strategy rests on (momentum persists over the
next window), not the strategy itself.

## Trade Journal

Both traders have always called `log_trade()` after a successful order, passing
the thesis, a confidence score and the signal readings behind the decision. But
they import it defensively:

```python
try:
    from tradejournal import log_trade
except ImportError:
    def log_trade(*args, **kwargs):
        pass          # <- this is what used to run
```

`tradejournal.py` did not exist, so the no-op won and all of that metadata was
discarded. It now exists: each call appends one JSON object to
`logs/trades.jsonl` (gitignored, override with `SIMMER_JOURNAL_PATH`).

```bash
./run.sh journal                      # per-bot summary
./run.sh journal --since 7d
./run.sh journal --source sdk:weather
./run.sh journal --recent 5           # latest thesis lines
python3 tradejournal.py --tail 3      # raw records
```

`log_trade()` never raises. It runs after an order has already executed, so an
exception there would turn a filled trade into a crashed run with no record of
it; failures warn on stderr and return `False`.

Two things to know about coverage:

- **The weather bot only journals live trades** (`not result.get("simulated")`),
  while the fastloop bot journals in paper mode too. So a paper-only history
  shows fastloop entries and no weather ones. That asymmetry is in the traders,
  not the journal — left as-is rather than changing trade behavior.
- **`journal` and `summary` answer different questions.** `summary` reads the
  run logs: what each invocation did, including every no-trade. `journal` reads
  the executed trades with their thesis and confidence attached.

## Reading the Logs

Both bots run quiet under cron, so `logs/` fills up with a long tail of
"no trade" lines. `./run.sh summary` rolls those up per log — run count, trades
executed, and a histogram of *why* it declined the rest of the time. That
histogram is the useful part: if one reason dominates, the corresponding
threshold in `config.json` is probably mistuned.

```bash
./run.sh summary                  # all logs, all history
./run.sh summary --since 24h      # last day only (also 90m, 7d)
./run.sh summary --json           # machine-readable, for dashboards
./run.sh summary --log logs/fastloop.log   # one log
```

Reads local files only — no API key required.

## Configuration

- `config.json` — fastloop bot settings (threshold, momentum, position size)
- `weather-trader/config.json` — weather bot settings (threshold, locations, max trades)

## File Structure

```
poly/
  run.sh                  # Entry point — picks which bot to run
  test_demo.py            # Interactive demo showing both bots
  fastloop_trader.py      # BTC 5-min fast market trader
  config.json             # Fastloop config
  requirements.txt        # Python dependencies
  tradejournal.py         # log_trade() — records executed trades to logs/
  scripts/
    lib/__init__.py       # Loads the traders by path to read their real schemas
    doctor.py             # Preflight checks for a deployment
    validate_config.py    # Validates both config.json files
    summarize_logs.py     # Roll up run logs (runs, trades, no-trade reasons)
    journal_report.py     # Report on recorded trades
    backtest_momentum.py  # Sweep min_momentum_pct over Binance history
  tests/
    test_validate_config.py
    test_summarize_logs.py
    test_tradejournal.py
  weather-trader/
    weather_trader.py     # NOAA weather trader
    config.json           # Weather config
    scripts/status.py     # Account status viewer
  logs/                   # Output logs (gitignored)
  .env                    # Your API key (gitignored)
  .env.example            # Template for .env
```
