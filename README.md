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

# 4. Test it works
python3 test_demo.py

# 5. Add cron jobs
crontab -e
```

Add these lines to your crontab:

```cron
# Weather bot every 30 minutes
*/30 * * * * /path/to/poly/run.sh weather >> /path/to/poly/logs/weather.log 2>&1

# Fastloop bot every 5 minutes
*/5  * * * * /path/to/poly/run.sh fastloop >> /path/to/poly/logs/fastloop.log 2>&1
```

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
  weather-trader/
    weather_trader.py     # NOAA weather trader
    config.json           # Weather config
    scripts/status.py     # Account status viewer
  logs/                   # Output logs (gitignored)
  .env                    # Your API key (gitignored)
  .env.example            # Template for .env
```
