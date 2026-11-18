"""What Wi-Fi users leave on this computer: their PDFs and results (kept until you delete them), an activity
log, and the devices you've blocked.

The sharing server runs in its own process and the window in another, so everything here lives in files that
both read: the blocked list is re-read whenever it changes on disk.
"""
import csv
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time

from .common import CONFIG_DIR

WORK = CONFIG_DIR / "wifi"                      # one folder per uploaded PDF
LOG_CSV = CONFIG_DIR / "wifi_activity.csv"      # opens in Excel
BLOCK_JSON = CONFIG_DIR / "wifi_blocked.json"   # ip -> {"device":…, "name":…, "since":…}; kept after restarts
COLS = ["Date", "Time", "IP address", "Device name", "MAC address", "Device", "Action", "Details"]
IS_WIN = os.name == "nt"
_lock = threading.Lock()
_names = {}                                     # ip -> (device name, mac), looked up once in the background


# ---------------------------------------------------------------- stored uploads
def folders():
    if not WORK.exists():
        return []
    return [p for p in WORK.iterdir() if p.is_dir()]


def stats():
    """(number of stored uploads, total bytes)."""
    n = size = 0
    for d in folders():
        n += 1
        for root, _dirs, files in os.walk(d):
            for f in files:
                try:
                    size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    return n, size


def delete_all():
    """Delete every stored upload with its results. Returns how many were deleted."""
    n = 0
    for d in folders():
        shutil.rmtree(d, ignore_errors=True)
        n += not d.exists()
    return n


def new_folder(uid, ip, name):
    """A readable folder per upload: 2026-11-18 22-41-05  192.168.0.12  report.pdf  1a2b3c"""
    stem = re.sub(r'[\\/:*?"<>|\s]+', " ", name).strip()[:40] or "upload"
    d = WORK / f"{time.strftime('%Y-%m-%d %H-%M-%S')}  {ip}  {stem}  {uid}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- who is it
def device(ua):
    """'iPhone · Safari', 'Android (SM-S918B) · Chrome', 'Windows PC · Edge' … from the browser's User-Agent."""
    ua = ua or ""
    if "iPhone" in ua:
        os_ = "iPhone"
    elif "iPad" in ua:
        os_ = "iPad"
    elif "Android" in ua:
        m = re.search(r"Android [\d.]+; (?:[a-z]{2}-[a-z]{2}; )?([^;)]+?)(?: Build|\))", ua)
        model = m.group(1).strip() if m else ""
        os_ = f"Android ({model})" if model and model != "K" else "Android"
    elif "Windows" in ua:
        os_ = "Windows PC"
    elif "Macintosh" in ua:
        os_ = "Mac"
    elif "CrOS" in ua:
        os_ = "Chromebook"
    elif "Linux" in ua:
        os_ = "Linux"
    else:
        os_ = "Unknown"
    for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("SamsungBrowser", "Samsung Internet"),
                      ("Firefox", "Firefox"), ("FxiOS", "Firefox"), ("CriOS", "Chrome"), ("Chrome", "Chrome"),
                      ("Safari", "Safari")):
        if key in ua:
            return f"{os_} · {name}"
    return os_


def _lookup(ip):
    name = mac = ""
    try:
        name = socket.gethostbyaddr(ip)[0]
        if name == ip:
            name = ""
    except Exception:
        pass
    try:
        cmd = ["arp", "-a", ip] if IS_WIN else ["arp", "-n", ip]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5,
                             creationflags=0x08000000 if IS_WIN else 0).stdout
        m = re.search(r"([0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2}", out)
        mac = m.group(0).replace("-", ":").upper() if m else ""
    except Exception:
        pass
    if ip in ("127.0.0.1", "::1"):
        name = name or "this computer"
    _names[ip] = (name, mac)


def identify(ip):
    """(device name, MAC address) for an IP: known straight away after the first visit."""
    if ip not in _names:
        _names[ip] = ("", "")
        threading.Thread(target=_lookup, args=(ip,), daemon=True).start()
    return _names[ip]


# ---------------------------------------------------------------- blocked devices
_blocked, _blocked_mtime = {}, None


def blocked():
    """{ip: {"device": …, "name": …, "since": "2026-11-18 10:02"}}, re-read when the file changes."""
    global _blocked, _blocked_mtime
    try:
        m = BLOCK_JSON.stat().st_mtime_ns
    except OSError:
        m = None
    if m != _blocked_mtime:
        try:
            _blocked = json.loads(BLOCK_JSON.read_text(encoding="utf-8")) if m is not None else {}
        except (OSError, ValueError):
            _blocked = {}
        _blocked_mtime = m
    return _blocked


def is_blocked(ip):
    return ip in blocked()


def _save_blocked(data):
    global _blocked_mtime
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BLOCK_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, BLOCK_JSON)
    _blocked_mtime = None          # read it back next time


def block(ip, device_="", name=""):
    data = dict(blocked())
    data[ip] = {"device": device_, "name": name, "since": time.strftime("%Y-%m-%d %H:%M")}
    _save_blocked(data)
    log_event(ip, "", "Blocked by you", device_)


def unblock(ip):
    data = dict(blocked())
    if data.pop(ip, None) is not None:
        _save_blocked(data)
        log_event(ip, "", "Unblocked by you")


# ---------------------------------------------------------------- activity log
def log_event(ip, ua, action, details="", wait=True):
    """Add a line to the activity log (in the background: the device name lookup can take a moment)."""
    identify(ip)
    now = time.localtime()

    def write():
        if wait:
            time.sleep(1.5)                      # give the name / MAC lookup a moment on a first visit
        name, mac = _names.get(ip, ("", ""))
        row = [time.strftime("%Y-%m-%d", now), time.strftime("%H:%M:%S", now), ip, name, mac,
               device(ua) if ua else "", action, details]
        try:
            with _lock:
                new = not LOG_CSV.exists()
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                with open(LOG_CSV, "a", newline="", encoding="utf-8-sig") as fh:
                    w = csv.writer(fh)
                    if new:
                        w.writerow(COLS)
                    w.writerow(row)
        except OSError:
            pass
    threading.Thread(target=write, daemon=True).start()


def read_log(limit=2000):
    """Newest first."""
    if not LOG_CSV.exists():
        return []
    with _lock:
        try:
            with open(LOG_CSV, newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.reader(fh))
        except OSError:
            return []
    rows = [r for r in rows[1:] if r]
    return rows[::-1][:limit]


def clear_log():
    with _lock:
        try:
            LOG_CSV.unlink()
        except FileNotFoundError:
            pass
