"""Build dl_pdf_tools for Windows.

    py -3.13 -m venv .venv
    .venv\\Scripts\\pip install -r requirements.txt
    .venv\\Scripts\\python build.py            # -> dist\\Windows\\dl_pdf_tools\\ and dist\\Windows\\dl_pdf_tools-windows.zip
"""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dlpt.common import APP_NAME, RELEASE_ASSET  # noqa: E402

DIST = ROOT / "dist" / "Windows"
ICON = ROOT / "assets" / "icon.ico"

# Qt modules the app never uses; leaving them out keeps the download much smaller.
EXCLUDE = ["PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtSql", "PySide6.QtTest",
           "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
           "PySide6.QtSvg", "PySide6.QtXml", "PySide6.QtConcurrent", "PySide6.QtDBus", "PySide6.QtPrintSupport",
           "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools", "tkinter", "unittest", "pydoc",
           # PyMuPDF extras the app doesn't use (table/layout AI models, numpy interop); Pillow is build-time only.
           "numpy", "PIL", "pymupdf.layout", "onnxruntime", "pymupdf4llm"]
# Files PyInstaller drags in anyway that the app never loads.
PRUNE = ["_internal/PySide6/opengl32sw.dll", "_internal/pymupdf/layout", "_internal/numpy.libs",
         "_internal/PySide6/translations"]


def make_icon():
    if ICON.exists():
        return
    from PIL import Image, ImageDraw
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # a red document with a folded corner
    d.polygon([(48, 16), (168, 16), (216, 64), (216, 240), (48, 240)], fill=(214, 48, 49))
    d.polygon([(168, 16), (168, 64), (216, 64)], fill=(245, 160, 160))
    # "PDF" bars
    for y, w in ((92, 120), (118, 96), (144, 120)):
        d.rounded_rectangle((72, y, 72 + w, y + 12), radius=6, fill=(255, 255, 255))
    # down arrow onto a bar = smaller file
    white = (255, 255, 255)
    d.rectangle((122, 168, 142, 186), fill=white)
    d.polygon([(100, 186), (164, 186), (132, 210)], fill=white)
    d.rounded_rectangle((88, 214, 176, 226), radius=6, fill=white)
    ICON.parent.mkdir(exist_ok=True)
    img.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.save(ICON.with_suffix(".png"))


def pyinstaller():
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--name", APP_NAME, "--icon", str(ICON),
           "--add-data", f"{ICON};assets",
           "--add-data", f"{ICON.with_suffix('.png')};assets",
           "--add-data", f"{ROOT / 'dlpt' / 'web_page.html'};dlpt",
           "--distpath", str(DIST), "--workpath", str(ROOT / "build"),
           "--specpath", str(ROOT / "build")]
    for m in EXCLUDE:
        cmd += ["--exclude-module", m]
    cmd += ["--collect-submodules", "zeroconf", "--collect-submodules", "qrcode", "--collect-data", "certifi"]
    subprocess.check_call(cmd + [str(ROOT / "run.py")])


def assemble():
    out = DIST / APP_NAME
    for rel in PRUNE:
        p = out / rel
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    for name in ("README.md", "LICENSE"):
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, out / name)
    # Double-click launcher for the browser version (starts the local server, opens the page).
    (out / f"{APP_NAME} (Web).cmd").write_text(
        f'@echo off\r\nstart "" "%~dp0{APP_NAME}.exe" --web\r\n', encoding="ascii")
    z = DIST / RELEASE_ASSET
    z.unlink(missing_ok=True)
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                zf.write(f, Path(APP_NAME) / f.relative_to(out))
    print(f"\nBuilt {out}\nZipped {z} ({z.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    make_icon()
    pyinstaller()
    assemble()
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    print(f"Run: {DIST / APP_NAME / (APP_NAME + '.exe')}")
