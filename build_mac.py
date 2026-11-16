"""Build dl_pdf_tools for macOS (run on a Mac; GitHub Actions does this automatically).

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python build_mac.py          # -> dist/MacOS/dl_pdf_tools-mac-<arch>.dmg

Builds for the Mac it runs on (Apple Silicon or Intel): makes the .app, smoke-tests it (every PDF tool in
the worker processes, the window, the browser version and Share via Wi-Fi), then packs a .dmg with the
app, the browser-version launcher and install notes.
"""

import json
import os
import platform
import plistlib
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dlpt.common import APP_NAME, APP_VERSION  # noqa: E402

ARCH = "apple-silicon" if platform.machine() == "arm64" else "intel"
DIST = ROOT / "dist" / "MacOS"
WORK = ROOT / "build-mac"
APP = WORK / "dist" / f"{APP_NAME}.app"
ICNS = ROOT / "assets" / "icon.icns"
BUNDLE_ID = "com.opengrapes.dl-pdf-tools"
DMG_NAME = f"{APP_NAME}-mac-{ARCH}.dmg"
CONFIG = Path.home() / "Library" / "Application Support" / APP_NAME

# Same trimming as the Windows build (build.py).
EXCLUDE = ["PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtSql", "PySide6.QtTest",
           "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
           "PySide6.QtSvg", "PySide6.QtXml", "PySide6.QtConcurrent", "PySide6.QtDBus", "PySide6.QtPrintSupport",
           "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools", "tkinter", "unittest", "pydoc",
           "numpy", "PIL", "pymupdf.layout", "onnxruntime", "pymupdf4llm"]


def sh(*cmd, **kw):
    print("$", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def make_icns():
    if ICNS.exists():
        return
    from PIL import Image
    img = Image.open(ROOT / "assets" / "icon.png").convert("RGBA").resize((1024, 1024), Image.LANCZOS)
    img.save(ICNS, format="ICNS")


def pyinstaller():
    shutil.rmtree(WORK, ignore_errors=True)
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--name", APP_NAME, "--icon", ICNS, "--osx-bundle-identifier", BUNDLE_ID,
           "--add-data", f"{ROOT / 'assets' / 'icon.png'}:assets",
           "--add-data", f"{ROOT / 'dlpt' / 'web_page.html'}:dlpt",
           "--collect-submodules", "zeroconf", "--collect-submodules", "qrcode", "--collect-data", "certifi",
           "--distpath", WORK / "dist", "--workpath", WORK / "work", "--specpath", WORK]
    for m in EXCLUDE:
        cmd += ["--exclude-module", m]
    sh(*cmd, ROOT / "run.py")


def fill_bundle():
    res = APP / "Contents" / "Resources"
    for name in ("LICENSE", "README.md"):
        shutil.copy2(ROOT / name, res / name)
    plist_path = APP / "Contents" / "Info.plist"
    with open(plist_path, "rb") as fh:
        pl = plistlib.load(fh)
    pl.update({
        "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": APP_VERSION, "CFBundleVersion": APP_VERSION,
        "LSMinimumSystemVersion": "13.0", "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,          # follow the Mac's light/dark setting
        # "Open With" / dropping a PDF on the Dock icon.
        "CFBundleDocumentTypes": [{"CFBundleTypeName": "PDF document", "CFBundleTypeRole": "Viewer",
                                   "LSHandlerRank": "Alternate", "LSItemContentTypes": ["com.adobe.pdf"]}],
        # Share via Wi-Fi: macOS asks once before the app may use the local network.
        "NSLocalNetworkUsageDescription": "Share via Wi-Fi lets you open dl_pdf_tools from your phone on the "
                                          "same Wi-Fi network.",
        "NSBonjourServices": ["_http._tcp"],
    })
    with open(plist_path, "wb") as fh:
        plistlib.dump(pl, fh)
    # Ad-hoc signature (required on Apple Silicon). Not notarized: first launch needs "Open Anyway".
    sh("codesign", "--force", "--deep", "--sign", "-", APP)
    sh("codesign", "--verify", "--deep", "--strict", APP)


WEB_COMMAND = f"""#!/bin/bash
# Opens the browser version of {APP_NAME} (runs only on this Mac).
for APP in "/Applications/{APP_NAME}.app" "$HOME/Applications/{APP_NAME}.app" "$(dirname "$0")/{APP_NAME}.app"; do
  if [ -x "$APP/Contents/MacOS/{APP_NAME}" ]; then
    nohup "$APP/Contents/MacOS/{APP_NAME}" --web >/dev/null 2>&1 &
    osascript -e 'tell application "Terminal" to close (every window whose name contains "{APP_NAME} (Web)")' >/dev/null 2>&1 &
    exit 0
  fi
done
osascript -e 'display alert "{APP_NAME} not found" message "Drag {APP_NAME} into Applications first."'
exit 1
"""

DMG_README = f"""{APP_NAME} {APP_VERSION} for macOS ({ARCH.replace('-', ' ')})

INSTALL
1. Drag {APP_NAME} onto the Applications folder.
2. Open Applications and double-click {APP_NAME}.
   macOS says it can't verify the developer: click Done.
3. Apple menu > System Settings > Privacy & Security, scroll down to Security:
   "{APP_NAME}" was blocked... click Open Anyway, then Open Anyway again and enter your password.
   (Only needed once.)

USE
- Open a PDF with the Open button, by dropping it on the window or the Dock icon,
  or right-click a PDF in Finder > Open With > {APP_NAME}.
- Split, Extract pages, Reduce size (with size estimates) and Organize (reorder, rotate, delete pages).
- Theme menu (top right): Match system, Light or Dark. Remembered on this Mac.
- Browser version: double-click "{APP_NAME} (Web).command" (right-click > Open the first time).
- Share via Wi-Fi: use the tools from your phone on the same Wi-Fi.
  macOS asks once to allow dl_pdf_tools to find devices on your local network: click Allow.
"""


def make_dmg():
    DIST.mkdir(parents=True, exist_ok=True)
    stage = WORK / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    sh("ditto", APP, stage / APP.name)              # ditto keeps symlinks and signatures intact
    os.symlink("/Applications", stage / "Applications")
    cmd = stage / f"{APP_NAME} (Web).command"
    cmd.write_text(WEB_COMMAND)
    cmd.chmod(0o755)
    (stage / "READ ME.txt").write_text(DMG_README)
    out = DIST / DMG_NAME
    out.unlink(missing_ok=True)
    sh("hdiutil", "create", "-volname", APP_NAME, "-srcfolder", stage, "-ov", "-format", "UDZO", out)
    print(f"\nBuilt {out} ({out.stat().st_size / 1e6:.0f} MB)")
    return out


# ---------------------------------------------------------------- smoke tests on the built app

def smoke_test():
    binary = APP / "Contents" / "MacOS" / APP_NAME
    t = WORK / "smoke"
    shutil.rmtree(t, ignore_errors=True)
    t.mkdir(parents=True)
    sys.path.insert(0, str(ROOT / "tests"))
    from test_engine import make_sample
    sample = t / "sample.pdf"
    make_sample(sample, 8)

    # Every PDF tool, run through the app's worker processes.
    p = subprocess.run([str(binary), "--selftest", str(sample), str(t / "out")], timeout=600)
    report = (t / "out" / "selftest.txt").read_text()
    print(report, flush=True)
    assert p.returncode == 0 and report.strip().endswith("OK"), "self-test failed"

    # The window opens and stays up.
    p = subprocess.Popen([str(binary), str(sample)])
    time.sleep(10)
    alive = p.poll() is None
    p.send_signal(signal.SIGTERM)
    p.wait(timeout=20)
    assert alive, "the window closed by itself (crash at start)"

    # Browser version answers on localhost.
    p = subprocess.Popen([str(binary), "--web", "--no-browser"])
    try:
        ok = False
        for _ in range(30):
            time.sleep(1)
            try:
                with urllib.request.urlopen("http://127.0.0.1:8775/ping", timeout=2) as r:
                    ok = APP_NAME.encode() in r.read()
                    break
            except OSError:
                continue
        assert ok, "browser version didn't start"
    finally:
        p.terminate()
        p.wait(timeout=20)

    # Share via Wi-Fi starts, answers, and stops when asked.
    state = CONFIG / "wifi.json"
    state.unlink(missing_ok=True)
    log = open(t / "wifi.log", "w+")
    p = subprocess.Popen([str(binary), "--wifi", str(os.getpid())], stdout=log, stderr=subprocess.STDOUT)
    try:
        st = None
        for _ in range(40):
            time.sleep(0.5)
            if state.exists():
                st = json.loads(state.read_text())
                break
        if not st or st.get("error"):
            log.seek(0)
            print("wifi output:", log.read(), flush=True)
            crash = CONFIG / "wifi.log"
            print("wifi.log:", crash.read_text() if crash.exists() else "(none)", flush=True)
            print("process exit code:", p.poll(), flush=True)
        assert st, "Share via Wi-Fi didn't start"
        for _ in range(30):   # the .local name is announced in the background
            if st.get("mdns") != "pending":
                break
            time.sleep(0.5)
            st = json.loads(state.read_text())
        print("Share via Wi-Fi .local name:", st.get("mdns"), flush=True)
        if st.get("error"):
            print(f"Share via Wi-Fi: {st['error']} (no network on this build machine; skipped)", flush=True)
        else:
            print("Share via Wi-Fi:", st["urls"], flush=True)
            with urllib.request.urlopen(f"http://127.0.0.1:{st['port']}/api/heartbeat", timeout=5) as r:
                assert r.status == 200
            req = urllib.request.Request(f"http://127.0.0.1:{st['port']}/api/quit", headers={"X-Quit": st["quit"]})
            urllib.request.urlopen(req, timeout=5).read()
            p.wait(timeout=15)
    finally:
        if p.poll() is None:
            p.terminate()
            p.wait(timeout=20)
    # Check for updates reaches GitHub from inside the app (certificates, network). The workflow passes the
    # build's own GitHub token, which can read this repo's releases while the repo is private.
    out = t / "update.json"
    subprocess.run([str(binary), "--check-update", str(out)], timeout=120)
    res = json.loads(out.read_text())
    print("Check for updates:", res, flush=True)
    assert "error" not in res and res.get("version"), f"update check failed: {res}"
    print("\nSMOKE TEST PASSED", flush=True)


def update_install_test(dmg):
    """Run the real updater against the new .dmg: replace a copy of the app while a process 'is' the app."""
    from dlpt import updater
    t = WORK / "update-test"
    shutil.rmtree(t, ignore_errors=True)
    t.mkdir(parents=True)
    old = t / f"{APP_NAME}.app"
    sh("ditto", APP, old)
    (old / "Contents" / "Resources" / "OLD_MARKER").write_text("old copy")
    dummy = subprocess.Popen(["sleep", "3"])
    cmd, work = updater.prepare_install(dmg, dummy.pid, launch=False, loc=old)
    updater.start_helper(cmd)
    for _ in range(120):
        if (work / "finished.txt").exists():
            break
        time.sleep(0.5)
    assert (work / "finished.txt").exists(), "the update helper didn't finish"
    assert not (old / "Contents" / "Resources" / "OLD_MARKER").exists(), "the app wasn't replaced"
    assert (old / "Contents" / "MacOS" / APP_NAME).exists(), "the replaced app has no program"
    sh("codesign", "--verify", "--deep", "--strict", old)
    print("UPDATE INSTALL TEST PASSED", flush=True)


if __name__ == "__main__":
    if sys.platform != "darwin":
        sys.exit("build_mac.py must run on macOS (GitHub Actions builds it: .github/workflows/mac.yml)")
    make_icns()
    pyinstaller()
    fill_bundle()
    smoke_test()
    update_install_test(make_dmg())
