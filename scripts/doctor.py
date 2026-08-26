#!/usr/bin/env python3
"""
Deployment Doctor

Preflight for a fresh box: checks the interpreter, the SDK, the API key, both
config files, the log directory, and the upstream APIs the bots depend on.
Run it once after following the VPS steps in the README and before adding the
cron entries - cron failures are silent, and a bot that dies on startup every
five minutes looks identical to a bot that simply found no trades.

Exit codes: 0 all clear (warnings allowed), 1 at least one FAIL.

Usage:
    python3 scripts/doctor.py
    python3 scripts/doctor.py --offline     # skip network checks
    python3 scripts/doctor.py --json
"""

import os
import sys
import json
import stat
import argparse
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import REPO_ROOT, TRADERS, display_path, load_trader_module  # noqa: E402
from validate_config import validate_bot  # noqa: E402

sys.stdout.reconfigure(line_buffering=True)

MIN_PYTHON = (3, 9)

ENDPOINTS = [
    ("Binance klines", "https://api.binance.com/api/v3/ping"),
    ("NOAA weather", "https://api.weather.gov/"),
    ("Simmer API", "https://api.simmer.markets"),
]

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


class Checks:
    def __init__(self):
        self.rows = []

    def add(self, status, name, detail=""):
        self.rows.append({"status": status, "check": name, "detail": detail})

    @property
    def failed(self):
        return [row for row in self.rows if row["status"] == FAIL]

    @property
    def warned(self):
        return [row for row in self.rows if row["status"] == WARN]


def check_python(checks):
    version = sys.version_info
    pretty = f"{version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) >= MIN_PYTHON:
        checks.add(PASS, "Python version", f"{pretty} at {sys.executable}")
    else:
        needed = ".".join(map(str, MIN_PYTHON))
        checks.add(FAIL, "Python version", f"{pretty} is below the required {needed}")


def check_sdk(checks):
    try:
        import simmer_sdk  # noqa: F401
    except ImportError:
        checks.add(FAIL, "simmer-sdk installed",
                   "not importable - run: pip install -r requirements.txt")
        return
    version = getattr(simmer_sdk, "__version__", "unknown version")
    checks.add(PASS, "simmer-sdk installed", str(version))


def check_env_file(checks):
    env_path = os.path.join(REPO_ROOT, ".env")
    if not os.path.isfile(env_path):
        checks.add(WARN, ".env present",
                   "missing - fine if SIMMER_API_KEY is exported another way "
                   "(cp .env.example .env)")
        return

    mode = stat.S_IMODE(os.stat(env_path).st_mode)
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        checks.add(WARN, ".env permissions",
                   f"mode {mode:04o} is readable by others - run: chmod 600 .env")
    else:
        checks.add(PASS, ".env permissions", f"mode {mode:04o}")

    checks.add(PASS, ".env present", display_path(env_path))


def check_gitignore(checks):
    path = os.path.join(REPO_ROOT, ".gitignore")
    if not os.path.isfile(path):
        checks.add(WARN, ".env is gitignored", "no .gitignore in the repo")
        return
    with open(path) as handle:
        entries = {line.strip() for line in handle}
    if ".env" in entries:
        checks.add(PASS, ".env is gitignored", "secrets stay local")
    else:
        checks.add(FAIL, ".env is gitignored",
                   ".gitignore does not list .env - your API key could be committed")


def check_api_key(checks):
    key = os.environ.get("SIMMER_API_KEY", "")
    if not key:
        # run.sh sources .env before invoking python, so an unset var here is
        # only a problem for direct `python3 scripts/...` runs
        checks.add(WARN, "SIMMER_API_KEY set",
                   "not in this environment - run via ./run.sh, which sources .env")
        return
    if not key.startswith("sk_"):
        checks.add(WARN, "SIMMER_API_KEY set",
                   "does not start with 'sk_' - check you copied the whole key")
        return
    checks.add(PASS, "SIMMER_API_KEY set", f"sk_...{key[-4:]} ({len(key)} chars)")


def check_traders(checks):
    for name in sorted(TRADERS):
        try:
            module = load_trader_module(name)
        except (ImportError, FileNotFoundError) as exc:
            checks.add(FAIL, f"{name} trader imports", str(exc))
            continue
        checks.add(PASS, f"{name} trader imports", display_path(module.__file__))


def check_configs(checks):
    for bot in ("fastloop", "weather"):
        result = validate_bot(bot)
        if result.errors:
            checks.add(FAIL, f"{bot} config valid", "; ".join(result.errors))
        elif result.warnings:
            checks.add(WARN, f"{bot} config valid", "; ".join(result.warnings))
        else:
            checks.add(PASS, f"{bot} config valid", result.config_display)


def check_logs(checks):
    logs = os.path.join(REPO_ROOT, "logs")
    if not os.path.isdir(logs):
        checks.add(WARN, "logs/ writable", "missing - run.sh creates it on first run")
        return
    probe = os.path.join(logs, ".doctor_write_probe")
    try:
        with open(probe, "w") as handle:
            handle.write("ok")
        os.remove(probe)
    except OSError as exc:
        checks.add(FAIL, "logs/ writable", f"cannot write to {display_path(logs)}: {exc}")
        return
    checks.add(PASS, "logs/ writable", display_path(logs))


def check_run_sh(checks):
    path = os.path.join(REPO_ROOT, "run.sh")
    if not os.path.isfile(path):
        checks.add(FAIL, "run.sh executable", "run.sh is missing")
        return
    if os.access(path, os.X_OK):
        checks.add(PASS, "run.sh executable", "cron can invoke it directly")
    else:
        checks.add(FAIL, "run.sh executable",
                   "not executable - run: chmod +x run.sh (cron entries would fail)")


def check_endpoints(checks):
    for label, url in ENDPOINTS:
        request = Request(url, headers={"User-Agent": "poly-doctor/1.0"})
        try:
            with urlopen(request, timeout=10) as response:
                code = response.getcode()
        except HTTPError as exc:
            # A 4xx from an unauthenticated probe still proves reachability
            if exc.code in (401, 403, 404):
                checks.add(PASS, f"{label} reachable", f"HTTP {exc.code} (endpoint is up)")
            else:
                checks.add(WARN, f"{label} reachable", f"HTTP {exc.code}")
            continue
        except (URLError, TimeoutError, OSError) as exc:
            checks.add(WARN, f"{label} reachable", f"{exc}")
            continue
        checks.add(PASS, f"{label} reachable", f"HTTP {code}")


def main():
    parser = argparse.ArgumentParser(description="Preflight checks for a poly deployment")
    parser.add_argument("--offline", action="store_true", help="Skip network checks")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = parser.parse_args()

    checks = Checks()
    check_python(checks)
    check_sdk(checks)
    check_env_file(checks)
    check_gitignore(checks)
    check_api_key(checks)
    check_traders(checks)
    check_configs(checks)
    check_logs(checks)
    check_run_sh(checks)
    if not args.offline:
        check_endpoints(checks)

    if args.json:
        print(json.dumps({
            "ok": not checks.failed,
            "failed": len(checks.failed),
            "warned": len(checks.warned),
            "checks": checks.rows,
        }, indent=2))
    else:
        print("=" * 68)
        print("POLY DOCTOR")
        print("=" * 68)
        width = max(len(row["check"]) for row in checks.rows)
        for row in checks.rows:
            print(f"  {row['status']}  {row['check'].ljust(width)}  {row['detail']}")
        print("=" * 68)
        total = len(checks.rows)
        print(f"  {total} checks: {len(checks.failed)} failed, {len(checks.warned)} warnings")
        if checks.failed:
            print("\n  Fix the FAIL rows before adding cron entries.")
        elif checks.warned:
            print("\n  No blockers. Review the WARN rows if something misbehaves.")
        else:
            print("\n  Ready to deploy.")
        print()

    sys.exit(1 if checks.failed else 0)


if __name__ == "__main__":
    main()
