"""Shared names, paths and settings for dl_pdf_tools."""

import json
import os
import sys
from pathlib import Path

APP_NAME = "dl_pdf_tools"
APP_VERSION = "1.6"
RELEASE_ASSET = f"{APP_NAME}-windows.zip"

FROZEN = getattr(sys, "frozen", False)
# Folder holding assets\ : next to the .exe when built, the project root when run from source.
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent.parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))

if sys.platform == "darwin":
    CONFIG_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
else:
    CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
SETTINGS_FILE = CONFIG_DIR / "settings.json"


def asset(name):
    for base in (APP_DIR / "assets", BUNDLE_DIR / "assets"):
        if (base / name).exists():
            return base / name
    return None


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
