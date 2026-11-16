"""dl_pdf_tools window: view a PDF, split it, extract pages and compress it."""

import bisect
import os
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

import pymupdf
from PySide6.QtCore import QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QDockWidget, QFileDialog, QFormLayout,
                               QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressDialog,
                               QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSpinBox, QStyle, QToolBar,
                               QVBoxLayout, QWidget)

from . import engine
from .common import APP_NAME, APP_VERSION, asset, human_size, load_settings, save_settings

PT = 96 / 72          # screen pixels per PDF point at 100 %
GAP, MARGIN = 14, 18  # space between pages / around them
ZOOMS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 6.0]
THUMB_W = 118


def render(page, scale, dpr=1.0):
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale * dpr, scale * dpr), alpha=False)
    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
    img.setDevicePixelRatio(dpr)
    return img


def show_in_folder(path):
    path = Path(path)
    if path.is_dir():
        os.startfile(str(path))
    else:
        subprocess.Popen(f'explorer /select,"{path}"')


# ======================================================================== viewer

class PageCanvas(QWidget):
    """Draws every page stacked vertically; only the pages on screen are rendered."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc, self.sizes, self.rects, self.tops = None, [], [], []
        self.zoom, self.avail_w = 1.0, 800
        self.cache = OrderedDict()
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    def set_doc(self, doc):
        self.doc = doc
        self.cache.clear()
        self.sizes = [(p.rect.width, p.rect.height) for p in doc] if doc else []
        self.relayout()

    def relayout(self):
        s = self.zoom * PT
        if not self.sizes:
            self.rects, self.tops = [], []
            self.setFixedSize(max(1, self.avail_w), 1)
            self.update()
            return
        maxw = max(w for w, _ in self.sizes) * s
        total_w = int(max(self.avail_w, maxw + 2 * MARGIN))
        y, rects = MARGIN, []
        for w, h in self.sizes:
            pw, ph = round(w * s), round(h * s)
            rects.append(QRect((total_w - pw) // 2, y, pw, ph))
            y += ph + GAP
        self.rects, self.tops = rects, [r.top() for r in rects]
        self.setFixedSize(total_w, y - GAP + MARGIN)
        self.update()

    def page_at(self, y):
        if not self.tops:
            return 0
        return max(0, min(len(self.tops) - 1, bisect.bisect_right(self.tops, y) - 1))

    def image(self, i):
        dpr = self.devicePixelRatioF()
        key = (i, self.zoom, dpr)
        img = self.cache.get(key)
        if img is None:
            try:
                img = render(self.doc[i], self.zoom * PT, dpr)
            except Exception:
                img = QImage()
            self.cache[key] = img
            while len(self.cache) > 30:
                self.cache.popitem(last=False)
        else:
            self.cache.move_to_end(key)
        return img

    def paintEvent(self, e):
        p = QPainter(self)
        area = e.rect()
        p.fillRect(area, self.palette().color(self.backgroundRole()).darker(115))
        if not self.rects:
            return
        start = self.page_at(area.top())
        for i in range(start, len(self.rects)):
            r = self.rects[i]
            if r.top() > area.bottom():
                break
            if r.bottom() < area.top():
                continue
            p.fillRect(r.translated(3, 3), QColor(0, 0, 0, 60))
            p.fillRect(r, Qt.GlobalColor.white)
            img = self.image(i)
            if not img.isNull():
                p.drawImage(r, img)


class PdfView(QScrollArea):
    pageChanged = Signal(int)
    zoomChanged = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.canvas = PageCanvas()
        self.setWidget(self.canvas)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.fit = "width"   # "width", "page" or None (fixed zoom)
        self._page = 0
        self.verticalScrollBar().valueChanged.connect(self._scrolled)
        self.verticalScrollBar().setSingleStep(40)

    # --- document
    def set_doc(self, doc):
        self._page = 0
        self.canvas.set_doc(doc)
        self._apply_fit()
        self.verticalScrollBar().setValue(0)
        self.pageChanged.emit(0)

    @property
    def page_count(self):
        return len(self.canvas.sizes)

    # --- zoom
    def set_zoom(self, z, fit=None):
        z = max(0.1, min(8.0, z))
        page, frac = self.current_page(), self._frac()
        self.fit = fit
        self.canvas.zoom = z
        self.canvas.avail_w = self.viewport().width()
        self.canvas.relayout()
        self._restore(page, frac)
        self.zoomChanged.emit(z)

    def zoom_step(self, direction):
        z = self.canvas.zoom
        if direction > 0:
            nz = next((v for v in ZOOMS if v > z + 1e-3), ZOOMS[-1])
        else:
            nz = next((v for v in reversed(ZOOMS) if v < z - 1e-3), ZOOMS[0])
        self.set_zoom(nz)

    def fit_width(self):
        self.fit = "width"
        self._apply_fit()

    def fit_page(self):
        self.fit = "page"
        self._apply_fit()

    def _apply_fit(self):
        sizes = self.canvas.sizes
        if not sizes or not self.fit:
            self.canvas.avail_w = self.viewport().width()
            self.canvas.relayout()
            return
        vw = self.viewport().width() - 2 * MARGIN - self.verticalScrollBar().sizeHint().width()
        vh = self.viewport().height() - 2 * MARGIN
        w, h = sizes[min(self.current_page(), len(sizes) - 1)]
        maxw = max(sw for sw, _ in sizes)
        z = vw / (maxw * PT) if self.fit == "width" else min(vw / (w * PT), vh / (h * PT))
        self.set_zoom(z, fit=self.fit)

    # --- navigation
    def current_page(self):
        return self._page

    def go_to(self, i):
        if 0 <= i < len(self.canvas.rects):
            self.verticalScrollBar().setValue(self.canvas.rects[i].top() - MARGIN // 2)

    def _frac(self):
        r = self.canvas.rects
        if not r:
            return 0.0
        rect = r[self._page]
        return (self.verticalScrollBar().value() - rect.top()) / max(1, rect.height())

    def _restore(self, page, frac):
        r = self.canvas.rects
        if r:
            rect = r[min(page, len(r) - 1)]
            self.verticalScrollBar().setValue(int(rect.top() + frac * rect.height()))

    def _scrolled(self, v):
        p = self.canvas.page_at(v + self.viewport().height() // 3)
        if p != self._page:
            self._page = p
            self.pageChanged.emit(p)

    # --- events
    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.fit:
            self._apply_fit()
        else:
            self.canvas.avail_w = self.viewport().width()
            self.canvas.relayout()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_step(1 if e.angleDelta().y() > 0 else -1)
            e.accept()
        else:
            super().wheelEvent(e)

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key.Key_PageDown, Qt.Key.Key_Right) and not self.horizontalScrollBar().isVisible():
            self.go_to(self._page + 1)
        elif k in (Qt.Key.Key_PageUp, Qt.Key.Key_Left) and not self.horizontalScrollBar().isVisible():
            self.go_to(self._page - 1)
        elif k == Qt.Key.Key_Home:
            self.go_to(0)
        elif k == Qt.Key.Key_End:
            self.go_to(self.page_count - 1)
        else:
            super().keyPressEvent(e)


class ThumbList(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setFlow(QListWidget.Flow.TopToBottom)
        self.setWrapping(False)
        self.setMovement(QListWidget.Movement.Static)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setIconSize(QSize(THUMB_W, int(THUMB_W * 1.42)))
        self.setSpacing(6)
        self.setUniformItemSizes(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.doc, self._next = None, 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._render_some)

    def set_doc(self, doc):
        self.timer.stop()
        self.clear()
        self.doc, self._next = doc, 0
        if not doc:
            return
        self.rendered = [False] * doc.page_count
        for i in range(doc.page_count):
            r = doc[i].rect
            w, h = self._fit(r.width, r.height)
            blank = QPixmap(w, h)
            blank.fill(Qt.GlobalColor.white)
            it = QListWidgetItem(QIcon(self._framed(blank)), str(i + 1))
            it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            it.setSizeHint(QSize(THUMB_W + 16, h + 30))
            self.addItem(it)
        self.timer.start(0)

    @staticmethod
    def _fit(w, h):
        s = min(THUMB_W / w, THUMB_W * 1.42 / h)
        return max(1, round(w * s)), max(1, round(h * s))

    @staticmethod
    def _framed(pm):
        p = QPainter(pm)
        p.setPen(QColor(150, 150, 150))
        r = pm.rect()
        p.drawRect(0, 0, round(r.width() / pm.devicePixelRatio()) - 1, round(r.height() / pm.devicePixelRatio()) - 1)
        p.end()
        return pm

    def _render_some(self):
        if not self.doc or self._next >= self.count():
            self.timer.stop()
            return
        # Render the ones on screen first, then the rest in order.
        todo = [i for i in self._visible_rows() if not self.rendered[i]][:3]
        while len(todo) < 3 and self._next < self.count():
            if not self.rendered[self._next] and self._next not in todo:
                todo.append(self._next)
            self._next += 1
        dpr = self.devicePixelRatioF()
        for i in todo:
            self.rendered[i] = True
            try:
                page = self.doc[i]
                w, _ = self._fit(page.rect.width, page.rect.height)
                pm = QPixmap.fromImage(render(page, w / page.rect.width, dpr))
                self.item(i).setIcon(QIcon(self._framed(pm)))
            except Exception:
                pass

    def _visible_rows(self):
        top = self.indexAt(self.viewport().rect().center())
        mid = top.row() if top.isValid() else 0
        return range(max(0, mid - 4), min(self.count(), mid + 5))

    def selected_pages(self):
        return sorted(self.row(it) for it in self.selectedItems())


# ======================================================================== background work

class Task(QThread):
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn(self.progress.emit))
        except Exception as e:  # shown to the user in a message box
            self.failed.emit(str(e) or e.__class__.__name__)


# ======================================================================== dialogs

class PathRow(QWidget):
    def __init__(self, path, mode, parent=None):
        super().__init__(parent)
        self.mode = mode  # "save" or "dir"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(str(path))
        btn = QPushButton("Browse…")
        btn.clicked.connect(self._browse)
        lay.addWidget(self.edit, 1)
        lay.addWidget(btn)

    def _browse(self):
        cur = self.edit.text().strip()
        if self.mode == "dir":
            p = QFileDialog.getExistingDirectory(self, "Choose folder", cur)
        else:
            p, _ = QFileDialog.getSaveFileName(self, "Save as", cur, "PDF files (*.pdf)")
        if p:
            self.edit.setText(os.path.normpath(p))

    def path(self):
        return Path(self.edit.text().strip().strip('"'))


def _note(text):
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet("color: palette(placeholder-text);")
    return lab


class SplitDialog(QDialog):
    def __init__(self, parent, src, page_count, ranges=""):
        super().__init__(parent)
        self.setWindowTitle("Split PDF")
        self.src, self.count = Path(src), page_count
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{self.src.name}</b> — {page_count} pages"))

        grid = QGridLayout()
        self.r_every = QRadioButton("Every")
        self.n = QSpinBox()
        self.n.setRange(1, max(1, page_count))
        self.n.setValue(min(max(1, page_count // 2), 10) if page_count > 1 else 1)
        self.r_each = QRadioButton("Each page into its own file")
        self.r_ranges = QRadioButton("Custom ranges")
        self.ranges = QLineEdit(ranges)
        self.ranges.setPlaceholderText("e.g. 1-3, 4-10, 11-")
        grid.addWidget(self.r_every, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.n)
        row.addWidget(QLabel("pages per file"))
        row.addStretch(1)
        grid.addLayout(row, 0, 1)
        grid.addWidget(self.r_each, 1, 0, 1, 2)
        grid.addWidget(self.r_ranges, 2, 0)
        grid.addWidget(self.ranges, 2, 1)
        lay.addLayout(grid)
        lay.addWidget(_note("Custom ranges: each comma-separated range becomes one file."))
        grp = QButtonGroup(self)
        for b in (self.r_every, self.r_each, self.r_ranges):
            grp.addButton(b)
        (self.r_ranges if ranges else self.r_every).setChecked(True)

        form = QFormLayout()
        self.out = PathRow(self.src.parent / f"{self.src.stem}_split", "dir")
        form.addRow("Save into folder:", self.out)
        lay.addLayout(form)
        self.preview = QLabel()
        lay.addWidget(self.preview)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.addButton("Split", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        for sig in (self.n.valueChanged, self.ranges.textChanged, grp.buttonToggled):
            sig.connect(self._update)
        self.ranges.textEdited.connect(lambda _: self.r_ranges.setChecked(True))
        self.n.valueChanged.connect(lambda _: self.r_every.setChecked(True))
        self.resize(520, self.sizeHint().height())
        self._update()

    def groups(self):
        if self.r_every.isChecked():
            n = self.n.value()
            return [list(range(i, min(i + n, self.count))) for i in range(0, self.count, n)]
        if self.r_each.isChecked():
            return [[i] for i in range(self.count)]
        return engine.parse_groups(self.ranges.text(), self.count)

    def _update(self, *_):
        try:
            g = self.groups()
            self.preview.setText(f"→ {len(g)} file{'s' if len(g) != 1 else ''}")
            self.ok.setEnabled(True)
        except engine.RangeError as e:
            self.preview.setText(f"<span style='color:#d33'>{e}</span>")
            self.ok.setEnabled(False)

    def _accept(self):
        try:
            self.result_groups = self.groups()
            self.out.path().mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "Split PDF", str(e))
            return
        self.accept()


class ExtractDialog(QDialog):
    def __init__(self, parent, src, page_count, pages):
        super().__init__(parent)
        self.setWindowTitle("Extract pages")
        self.src, self.count = Path(src), page_count
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{self.src.name}</b> — {page_count} pages"))

        form = QFormLayout()
        self.pages = QLineEdit(engine.format_ranges(pages))
        self.pages.setPlaceholderText("e.g. 1-3, 5, 8-")
        form.addRow("Pages:", self.pages)
        lay.addLayout(form)
        lay.addWidget(_note("Tip: select pages in the thumbnail list (Ctrl / Shift + click) before opening this."))

        self.r_one = QRadioButton("Into one new PDF")
        self.r_each = QRadioButton("Each page as a separate PDF")
        self.r_one.setChecked(True)
        lay.addWidget(self.r_one)
        self.save = PathRow("", "save")
        lay.addWidget(self.save)
        lay.addWidget(self.r_each)
        self.folder = PathRow(self.src.parent / f"{self.src.stem}_pages", "dir")
        lay.addWidget(self.folder)
        self.preview = QLabel()
        lay.addWidget(self.preview)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.addButton("Extract", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self._name_edited = False
        self.save.edit.textEdited.connect(lambda _: setattr(self, "_name_edited", True))
        self.pages.textChanged.connect(self._update)
        self.r_one.toggled.connect(self._update)
        self.resize(540, self.sizeHint().height())
        self._update()

    def _update(self, *_):
        one = self.r_one.isChecked()
        self.save.setEnabled(one)
        self.folder.setEnabled(not one)
        try:
            pages = engine.parse_pages(self.pages.text(), self.count)
        except engine.RangeError as e:
            self.preview.setText(f"<span style='color:#d33'>{e}</span>")
            self.ok.setEnabled(False)
            return
        self.ok.setEnabled(True)
        n = len(pages)
        self.preview.setText(f"→ {n} page{'s' if n != 1 else ''}" +
                             ("" if one else f" in {n} file{'s' if n != 1 else ''}"))
        if not self._name_edited:
            label = engine.format_ranges(pages).replace(", ", "_").replace(" ", "")
            if len(label) > 40:
                label = label[:40] + "_etc"
            self.save.edit.setText(str(self.src.parent / f"{self.src.stem}_pages_{label}.pdf"))

    def _accept(self):
        try:
            self.result_pages = engine.parse_pages(self.pages.text(), self.count)
            self.result_one = self.r_one.isChecked()
            if self.result_one:
                p = self.save.path()
                if p.suffix.lower() != ".pdf":
                    p = p.with_suffix(".pdf")
                if p.resolve() == self.src.resolve():
                    raise ValueError("Choose a different file name; that is the PDF you have open.")
                if p.exists() and QMessageBox.question(self, "Extract pages", f"{p.name} already exists. Replace it?") \
                        != QMessageBox.StandardButton.Yes:
                    return
                p.parent.mkdir(parents=True, exist_ok=True)
                if p.exists():
                    p.unlink()
                self.result_path = p
            else:
                self.result_path = self.folder.path()
                self.result_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "Extract pages", str(e))
            return
        self.accept()


class CompressDialog(QDialog):
    def __init__(self, parent, src, settings):
        super().__init__(parent)
        self.setWindowTitle("Reduce file size")
        self.src = Path(src)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{self.src.name}</b> — currently {human_size(self.src.stat().st_size)}"))

        self.grp = QButtonGroup(self)
        self.radios = {}
        for key, pr in engine.PRESETS.items():
            rb = QRadioButton(f"{pr['label']}   ({pr['dpi']} dpi images, quality {pr['quality']})")
            self.radios[key] = rb
            self.grp.addButton(rb)
            lay.addWidget(rb)
        rb = QRadioButton("Custom")
        self.radios["custom"] = rb
        self.grp.addButton(rb)
        row = QHBoxLayout()
        row.addWidget(rb)
        self.dpi = QSpinBox()
        self.dpi.setRange(36, 600)
        self.dpi.setSuffix(" dpi")
        self.quality = QSpinBox()
        self.quality.setRange(10, 100)
        self.quality.setPrefix("quality ")
        row.addWidget(self.dpi)
        row.addWidget(self.quality)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(_note("Pictures and scans larger than the chosen resolution are scaled down and re-saved as "
                            "JPEG. Text and vector graphics stay sharp. Fonts are trimmed to the characters used "
                            "and unused data is removed."))

        self.gray = QCheckBox("Convert images to black && white (grayscale)")
        self.meta = QCheckBox("Remove document properties (title, author, …)")
        self.no_img = QCheckBox("Don't touch images (lossless only)")
        for c in (self.gray, self.meta, self.no_img):
            lay.addWidget(c)

        form = QFormLayout()
        self.out = PathRow(self.src.parent / f"{self.src.stem}_compressed.pdf", "save")
        form.addRow("Save as:", self.out)
        lay.addLayout(form)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        bb.addButton("Compress", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        c = settings.get("compress", {})
        self.radios.get(c.get("preset", "balanced"), self.radios["balanced"]).setChecked(True)
        self.dpi.setValue(c.get("dpi", 150))
        self.quality.setValue(c.get("quality", 72))
        self.gray.setChecked(c.get("gray", False))
        self.meta.setChecked(c.get("meta", False))
        self.no_img.setChecked(c.get("no_img", False))
        self.dpi.valueChanged.connect(lambda _: self.radios["custom"].setChecked(True))
        self.quality.valueChanged.connect(lambda _: self.radios["custom"].setChecked(True))
        self.no_img.toggled.connect(self._update)
        self._update()
        self.resize(560, self.sizeHint().height())

    def _update(self, *_):
        on = not self.no_img.isChecked()
        for w in list(self.radios.values()) + [self.dpi, self.quality, self.gray]:
            w.setEnabled(on)

    def preset(self):
        return next(k for k, rb in self.radios.items() if rb.isChecked())

    def options(self):
        key = self.preset()
        if key == "custom":
            dpi, q, thr = self.dpi.value(), self.quality.value(), int(self.dpi.value() * 1.3)
        else:
            pr = engine.PRESETS[key]
            dpi, q, thr = pr["dpi"], pr["quality"], pr["threshold"]
        return {"dpi": dpi, "quality": q, "threshold": thr, "gray": self.gray.isChecked(),
                "strip_meta": self.meta.isChecked(), "images": not self.no_img.isChecked()}

    def remember(self):
        return {"preset": self.preset(), "dpi": self.dpi.value(), "quality": self.quality.value(),
                "gray": self.gray.isChecked(), "meta": self.meta.isChecked(), "no_img": self.no_img.isChecked()}

    def _accept(self):
        p = self.out.path()
        if p.suffix.lower() != ".pdf":
            p = p.with_suffix(".pdf")
        try:
            if p.resolve() == self.src.resolve():
                raise ValueError("Choose a different file name; the original PDF is kept unchanged.")
            if p.exists() and QMessageBox.question(self, "Reduce file size", f"{p.name} already exists. Replace it?") \
                    != QMessageBox.StandardButton.Yes:
                return
            p.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "Reduce file size", str(e))
            return
        self.result_path = p
        self.accept()


# ======================================================================== main window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.doc, self.path, self.password, self.task = None, None, None, None
        self.setAcceptDrops(True)
        ico = asset("icon.ico")
        if ico:
            self.setWindowIcon(QIcon(str(ico)))

        self.view = PdfView()
        self.setCentralWidget(self.view)
        self.empty = QLabel("Open a PDF  (Ctrl+O)  or drop one here", self.view)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("font-size: 15px; color: palette(placeholder-text);")

        self.thumbs = ThumbList()
        self.thumbs.itemClicked.connect(lambda it: self.view.go_to(self.thumbs.row(it)))
        self.thumbs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.thumbs.customContextMenuRequested.connect(self._thumb_menu)
        self.dock = QDockWidget("Pages", self)
        self.dock.setObjectName("pages")
        self.dock.setWidget(self.thumbs)
        self.dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.dock.setMinimumWidth(THUMB_W + 50)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock)
        self.resizeDocks([self.dock], [THUMB_W + 60], Qt.Orientation.Horizontal)

        self._build_toolbar()
        self.view.pageChanged.connect(self._page_changed)
        self.view.zoomChanged.connect(self._zoom_changed)
        self.status = self.statusBar()
        self.info = QLabel()
        self.status.addPermanentWidget(self.info)

        g = self.settings.get("geometry")
        if g:
            self.restoreGeometry(bytes.fromhex(g))
        else:
            self.resize(1200, 860)
        self._set_enabled(False)
        self._title()

    # ---------------------------------------------------------------- UI building
    def _act(self, text, slot, shortcut=None, icon=None, tip=None):
        a = QAction(text, self)
        if icon is not None:
            a.setIcon(self.style().standardIcon(icon))
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.setToolTip(f"{tip or text}" + (f"  ({QKeySequence(shortcut).toString()})" if shortcut else ""))
        a.triggered.connect(slot)
        self.addAction(a)
        return a

    def _build_toolbar(self):
        SP = QStyle.StandardPixmap
        tb = QToolBar("Main", self)
        tb.setObjectName("main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(tb)

        self.a_open = self._act("Open", self.choose_file, "Ctrl+O", SP.SP_DialogOpenButton, "Open a PDF")
        tb.addAction(self.a_open)
        self.a_pages = self.dock.toggleViewAction()
        self.a_pages.setText("Pages")
        self.a_pages.setShortcut(QKeySequence("F4"))
        self.a_pages.setIcon(self.style().standardIcon(SP.SP_FileDialogListView))
        tb.addAction(self.a_pages)
        tb.addSeparator()

        self.a_prev = self._act("", lambda: self.view.go_to(self.view.current_page() - 1), None, SP.SP_ArrowUp,
                                "Previous page")
        self.a_next = self._act("", lambda: self.view.go_to(self.view.current_page() + 1), None, SP.SP_ArrowDown,
                                "Next page")
        tb.addAction(self.a_prev)
        self.page_box = QSpinBox()
        self.page_box.setRange(1, 1)
        self.page_box.setKeyboardTracking(False)
        self.page_box.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.page_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_box.setMinimumWidth(52)
        self.page_box.valueChanged.connect(lambda v: self.view.go_to(v - 1) if v - 1 != self.view.current_page() else None)
        tb.addWidget(self.page_box)
        self.of_label = QLabel(" / 0 ")
        tb.addWidget(self.of_label)
        tb.addAction(self.a_next)
        tb.addSeparator()

        self.a_zout = self._act("", lambda: self.view.zoom_step(-1), "Ctrl+-", None, "Zoom out")
        self.a_zout.setText("−")
        self.a_zin = self._act("", lambda: self.view.zoom_step(1), "Ctrl+=", None, "Zoom in")
        self.a_zin.setText("+")
        zin_alt = QAction(self)  # Ctrl + numpad plus
        zin_alt.setShortcut(QKeySequence("Ctrl++"))
        zin_alt.triggered.connect(lambda: self.view.zoom_step(1))
        self.addAction(zin_alt)
        tb.addAction(self.a_zout)
        self.zoom_box = QComboBox()
        self.zoom_box.setEditable(True)
        self.zoom_box.setMinimumWidth(80)
        self.zoom_box.addItems([f"{round(z * 100)}%" for z in ZOOMS])
        self.zoom_box.lineEdit().returnPressed.connect(self._zoom_typed)
        self.zoom_box.activated.connect(lambda i: self.view.set_zoom(ZOOMS[i]))
        tb.addWidget(self.zoom_box)
        tb.addAction(self.a_zin)
        self.a_fitw = self._act("Fit width", self.view.fit_width, "Ctrl+0")
        self.a_fitp = self._act("Fit page", self.view.fit_page, "Ctrl+9")
        tb.addAction(self.a_fitw)
        tb.addAction(self.a_fitp)
        tb.addSeparator()

        self.a_split = self._act("Split", self.split, "Ctrl+Shift+S", SP.SP_FileDialogDetailedView,
                                 "Split into several PDFs")
        self.a_extract = self._act("Extract pages", self.extract, "Ctrl+E", SP.SP_FileDialogNewFolder,
                                   "Save chosen pages as a new PDF")
        self.a_compress = self._act("Reduce size", self.compress, "Ctrl+R", SP.SP_DriveHDIcon,
                                    "Make a smaller copy of this PDF")
        for a in (self.a_split, self.a_extract, self.a_compress):
            tb.addAction(a)
            tb.widgetForAction(a).setStyleSheet("font-weight: 600;")

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.a_folder = self._act("", self._show_current, None, SP.SP_DirOpenIcon, "Show file in Explorer")
        tb.addAction(self.a_folder)
        self.a_about = self._act("", self.about, "F1", SP.SP_MessageBoxInformation, "About")
        tb.addAction(self.a_about)

    def _set_enabled(self, on):
        for a in (self.a_prev, self.a_next, self.a_zin, self.a_zout, self.a_fitw, self.a_fitp, self.a_split,
                  self.a_extract, self.a_compress, self.a_folder):
            a.setEnabled(on)
        self.page_box.setEnabled(on)
        self.zoom_box.setEnabled(on)
        self.empty.setVisible(not on)

    def _title(self):
        self.setWindowTitle(f"{self.path.name} — {APP_NAME}" if self.path else APP_NAME)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.empty.setGeometry(self.view.rect())

    # ---------------------------------------------------------------- open / close
    def choose_file(self):
        start = self.settings.get("last_dir") or str(Path.home() / "Documents")
        p, _ = QFileDialog.getOpenFileName(self, "Open PDF", start, "PDF files (*.pdf);;All files (*)")
        if p:
            self.open_file(p)

    def open_file(self, path):
        path = Path(path)
        try:
            doc = pymupdf.open(str(path))
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't open {path.name}:\n{e}")
            return
        if not doc.is_pdf:
            doc.close()
            QMessageBox.warning(self, APP_NAME, f"{path.name} is not a PDF.")
            return
        password = None
        while doc.needs_pass:
            pw, ok = QInputDialog.getText(self, "Password required", f"{path.name} is protected. Password:",
                                          QLineEdit.EchoMode.Password)
            if not ok:
                doc.close()
                return
            if doc.authenticate(pw):
                password = pw
        self.close_doc()
        self.doc, self.path, self.password = doc, path, password
        self.view.set_doc(doc)
        self.thumbs.set_doc(doc)
        self.page_box.setRange(1, doc.page_count)
        self.page_box.setValue(1)
        self.of_label.setText(f" / {doc.page_count} ")
        self.info.setText(f"{doc.page_count} pages · {human_size(path.stat().st_size)}")
        self.settings["last_dir"] = str(path.parent)
        self._set_enabled(True)
        self._title()
        self.view.setFocus()

    def close_doc(self):
        self.thumbs.set_doc(None)
        self.view.set_doc(None)
        if self.doc:
            self.doc.close()
        self.doc, self.path, self.password = None, None, None
        self._set_enabled(False)
        self.info.clear()
        self._title()

    # ---------------------------------------------------------------- view callbacks
    def _page_changed(self, i):
        self.page_box.blockSignals(True)
        self.page_box.setValue(i + 1)
        self.page_box.blockSignals(False)
        if len(self.thumbs.selectedItems()) <= 1 and i < self.thumbs.count():
            self.thumbs.setCurrentRow(i)
            self.thumbs.scrollToItem(self.thumbs.item(i))

    def _zoom_changed(self, z):
        self.zoom_box.setEditText(f"{round(z * 100)}%")

    def _zoom_typed(self):
        t = self.zoom_box.currentText().strip().rstrip("%").strip()
        try:
            self.view.set_zoom(float(t) / 100)
        except ValueError:
            self._zoom_changed(self.view.canvas.zoom)

    def _thumb_menu(self, pos):
        if not self.doc:
            return
        it = self.thumbs.itemAt(pos)
        sel = self.thumbs.selected_pages()
        m = QMenu(self)
        if sel:
            n = len(sel)
            m.addAction(f"Extract {n} selected page{'s' if n > 1 else ''}…", lambda: self.extract(sel))
        if it is not None:
            row = self.thumbs.row(it)
            if row < self.doc.page_count - 1:
                m.addAction(f"Split after page {row + 1}…",
                            lambda: self.split(f"1-{row + 1}, {row + 2}-{self.doc.page_count}"))
        m.addSeparator()
        m.addAction("Select all", self.thumbs.selectAll)
        m.exec(self.thumbs.viewport().mapToGlobal(pos))

    # ---------------------------------------------------------------- tools
    def _run(self, title, fn, on_done):
        dlg = QProgressDialog(title, None, 0, 0, self)
        dlg.setWindowTitle(APP_NAME)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setMinimumWidth(360)
        dlg.show()
        self.task = Task(fn, self)
        self.task.progress.connect(dlg.setLabelText)

        def finished(result):
            dlg.close()
            on_done(result)

        def failed(msg):
            dlg.close()
            QMessageBox.critical(self, APP_NAME, f"{title.rstrip('…')} failed:\n\n{msg}")

        self.task.done.connect(finished)
        self.task.failed.connect(failed)
        self.task.start()

    def _report(self, title, text, reveal, open_pdf=None):
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(text)
        b_show = box.addButton("Show in folder", QMessageBox.ButtonRole.ActionRole)
        b_open = box.addButton("Open it here", QMessageBox.ButtonRole.ActionRole) if open_pdf else None
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is b_show:
            show_in_folder(reveal)
        elif b_open is not None and box.clickedButton() is b_open:
            self.open_file(open_pdf)

    def split(self, ranges=""):
        if not self.doc:
            return
        d = SplitDialog(self, self.path, self.doc.page_count, ranges if isinstance(ranges, str) else "")
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        src, pw, groups, out = self.path, self.password, d.result_groups, d.out.path()

        def work(report):
            return engine.split_groups(src, groups, out, pw, lambda i, n: report(f"Writing file {i} of {n}…"))

        self._run("Splitting…", work, lambda files: self._report(
            "Split PDF", f"Created {len(files)} file{'s' if len(files) != 1 else ''} in\n{out}", out))

    def extract(self, pages=None):
        if not self.doc:
            return
        if not isinstance(pages, list) or not pages:
            pages = self.thumbs.selected_pages() if len(self.thumbs.selectedItems()) > 1 else [self.view.current_page()]
        d = ExtractDialog(self, self.path, self.doc.page_count, pages)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        src, pw, sel, out = self.path, self.password, d.result_pages, d.result_path
        n = len(sel)
        if d.result_one:
            self._run("Extracting pages…", lambda _r: engine.extract(src, sel, out, pw), lambda p: self._report(
                "Extract pages", f"Saved {n} page{'s' if n != 1 else ''} to\n{p}", p, open_pdf=p))
        else:
            self._run("Extracting pages…",
                      lambda r: engine.extract_each(src, sel, out, pw, lambda i, t: r(f"Writing page {i} of {t}…")),
                      lambda files: self._report("Extract pages", f"Created {len(files)} files in\n{out}", out))

    def compress(self):
        if not self.doc:
            return
        d = CompressDialog(self, self.path, self.settings)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        self.settings["compress"] = d.remember()
        src, pw, out, opts = self.path, self.password, d.result_path, d.options()

        def done(r):
            if not r["written"]:
                QMessageBox.information(self, "Reduce file size",
                                        f"{src.name} is already well optimised ({human_size(r['before'])}).\n\n"
                                        "These settings couldn't make it smaller, so no new file was written. "
                                        "Try the Strong preset or a lower custom resolution.")
                return
            saved = 100 * (1 - r["after"] / r["before"])
            self._report("Reduce file size",
                         f"{human_size(r['before'])}  →  {human_size(r['after'])}   ({saved:.0f}% smaller)\n\n"
                         f"Saved as {r['path']}", r["path"], open_pdf=r["path"])

        self._run("Compressing…", lambda rep: engine.compress(src, out, password=pw, progress=rep, **opts), done)

    def _show_current(self):
        if self.path:
            show_in_folder(self.path)

    def about(self):
        QMessageBox.about(self, f"About {APP_NAME}",
                          f"<b>{APP_NAME}</b> {APP_VERSION}<br><br>View PDFs, split them, extract pages and "
                          f"reduce their file size. Your original files are never changed.<br><br>"
                          f"Built with PyMuPDF {pymupdf.VersionBind} (AGPL-3.0) and Qt for Python.<br><br>"
                          "<b>Shortcuts</b><br>Ctrl+O open · Ctrl+E extract · Ctrl+Shift+S split · Ctrl+R reduce "
                          "size<br>Ctrl+wheel or Ctrl +/− zoom · Ctrl+0 fit width · Ctrl+9 fit page · F4 pages panel")

    # ---------------------------------------------------------------- drag & drop / close
    def dragEnterEvent(self, e):
        if any(u.toLocalFile().lower().endswith(".pdf") for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            if u.toLocalFile().lower().endswith(".pdf"):
                self.open_file(u.toLocalFile())
                break

    def closeEvent(self, e):
        if self.task is not None and self.task.isRunning():
            QMessageBox.information(self, APP_NAME, "Please wait for the current job to finish.")
            e.ignore()
            return
        self.settings["geometry"] = bytes(self.saveGeometry()).hex()
        save_settings(self.settings)
        self.close_doc()
        super().closeEvent(e)


def selftest(src, out_dir):
    """`dl_pdf_tools.exe --selftest file.pdf folder` runs every operation without the window; for checking builds."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = []
    try:
        with pymupdf.open(src) as d:
            n = d.page_count
        log.append(f"pages {n}")
        log.append(f"extract {engine.extract(src, [0], out_dir / 'extract.pdf')}")
        log.append(f"split {len(engine.split_every(src, max(1, n // 2), out_dir))} files")
        r = engine.compress(src, out_dir / "compressed.pdf", dpi=150, quality=72)
        log.append(f"compress {r['before']} -> {r['after']} written={r['written']}")
        log.append("OK")
    except Exception as e:
        import traceback
        log.append(traceback.format_exc())
        log.append(f"FAILED {e}")
    (out_dir / "selftest.txt").write_text("\n".join(log), encoding="utf-8")
    return 0 if log[-1] == "OK" else 1


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) >= 4 and argv[1] == "--selftest":
        return selftest(argv[2], argv[3])
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"dl.{APP_NAME}")
        except Exception:
            pass
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    ico = asset("icon.ico")
    if ico:
        app.setWindowIcon(QIcon(str(ico)))
    win = MainWindow()
    win.show()
    files = [a for a in argv[1:] if not a.startswith("-")]
    if files:
        QTimer.singleShot(0, lambda: win.open_file(files[0]))
    return app.exec()
