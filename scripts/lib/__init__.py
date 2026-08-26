"""
Shared helpers for the scripts in this directory.

The traders live at the repo root and under weather-trader/, and neither is an
importable package (the directory name has a hyphen). load_trader_module() pulls
them in by file path so tooling can read their real CONFIG_SCHEMA rather than
keeping a second copy that drifts.

Importing either trader is side-effect safe: they build their config at module
level from files and env vars, and defer SDK/API-key work to get_client().
"""

import os
import sys
import importlib.util

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TRADERS = {
    "fastloop": os.path.join(REPO_ROOT, "fastloop_trader.py"),
    "weather": os.path.join(REPO_ROOT, "weather-trader", "weather_trader.py"),
}

# config.json each trader reads, relative to the repo root
CONFIG_PATHS = {
    "fastloop": os.path.join(REPO_ROOT, "config.json"),
    "weather": os.path.join(REPO_ROOT, "weather-trader", "config.json"),
}


def load_trader_module(name):
    """Import a trader by file path and return the module.

    Raises FileNotFoundError if the trader is missing, ImportError if it fails
    to execute.
    """
    if name not in TRADERS:
        raise KeyError(f"Unknown trader {name!r} (expected one of {sorted(TRADERS)})")

    path = TRADERS[name]
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Trader not found: {path}")

    module_name = f"_poly_trader_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - surface any import failure as ImportError
        del sys.modules[module_name]
        raise ImportError(f"Failed to import {path}: {exc}") from exc
    return module


def display_path(path):
    """Repo-relative for paths inside the repo, absolute for anything else."""
    absolute = os.path.abspath(path)
    if absolute.startswith(REPO_ROOT + os.sep):
        return os.path.relpath(absolute, REPO_ROOT)
    return absolute
