"""dl_pdf_tools window: view a PDF, split it, extract pages and compress it."""

import bisect
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path

import pymupdf
from PySide6.QtCore import QObject, QPoint, QRect, QRectF, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QActionGroup, QColor, QDesktopServices, QIcon, QImage, QKeySequence, QPainter,
                           QPalette, QPixmap,
                           QTransform)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QDockWidget, QFileDialog, QFormLayout,
                               QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar, QProgressDialog,
                               QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QStyle,
                               QTableWidget, QTableWidgetItem, QTextBrowser, QToolBar, QToolButton,
                               QVBoxLayout, QWidget)

from . import engine, jobs, updater, wifi_store
from .common import APP_NAME, APP_VERSION, asset, human_size, load_settings, save_settings

PT = 96 / 72          # screen pixels per PDF point at 100 %
GAP, MARGIN = 14, 18  # space between pages / around them
ZOOMS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 6.0]
THUMB_W = 118
WARN_COLOR = "#d18616"   # reminders (readable in light and dark)


def render(page, scale, dpr=1.0):
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale * dpr, scale * dpr), alpha=False)
    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
    img.setDevicePixelRatio(dpr)
    return img


def show_in_folder(path):
    path = Path(path)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)] if path.is_dir() else ["open", "-R", str(path)])
        return
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

class Bridge(QObject):
    """Delivers results from worker processes back to the GUI thread."""
    arrived = Signal(object, object, object)   # future, on_done, on_error

    def __init__(self):
        super().__init__()
        self.arrived.connect(self._deliver)

    def submit(self, fn, *args, on_done=None, on_error=None, **kwargs):
        fut = jobs.submit(fn, *args, **kwargs)
        fut.add_done_callback(lambda f: self.arrived.emit(f, on_done, on_error))
        return fut

    @staticmethod
    def _deliver(fut, on_done, on_error):
        if fut.cancelled():
            return
        try:
            exc = fut.exception()
            if exc is not None:
                if on_error:
                    on_error(str(exc) or exc.__class__.__name__)
            elif on_done:
                on_done(fut.result())
        except RuntimeError:
            pass  # the dialog that asked for this result has been closed


_bridge = None


def bridge():
    global _bridge
    if _bridge is None:
        _bridge = Bridge()
    return _bridge


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
    lab.setForegroundRole(QPalette.ColorRole.PlaceholderText)
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
    """Extract pages. `entries` is the current page arrangement (plain page numbers, or Organize's
    (page, rotation) list); the page box and `positions` refer to positions in it, 0-based."""

    def __init__(self, parent, src, entries, positions, password=None, saved=None):
        super().__init__(parent)
        self.setWindowTitle("Extract pages")
        self.src, self.entries, self.password = Path(src), list(entries), password
        self.count = len(self.entries)
        self.saved, self.cvalues = saved or {}, None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{self.src.name}</b> — {self.count} pages"))

        n = len(positions)
        if n > 1:
            q = QLabel(f"You selected <b>{n} pages</b>. Do you want them combined into one PDF, "
                       "or each page saved as its own PDF?")
            q.setWordWrap(True)
            q.setStyleSheet("font-size: 14px; padding: 4px 0 6px 0;")
            lay.addWidget(q)

        form = QFormLayout()
        self.pages = QLineEdit(engine.format_ranges(positions))
        self.pages.setPlaceholderText("e.g. 1-3, 5, 8-")
        form.addRow("Pages:", self.pages)
        lay.addLayout(form)
        lay.addWidget(_note("Tip: select pages in the Pages panel or in Organize (Ctrl / Shift + click) "
                            "before opening this."))

        self.r_one = QRadioButton("Combine into one PDF")
        self.r_each = QRadioButton("Save each page as its own PDF")
        f = self.r_one.font()
        f.setBold(True)
        self.r_one.setFont(f)
        self.r_each.setFont(f)
        grp = QButtonGroup(self)
        grp.addButton(self.r_one)
        grp.addButton(self.r_each)
        (self.r_each if self.saved.get("extract_each") else self.r_one).setChecked(True)
        lay.addWidget(self.r_one)
        self.save = PathRow("", "save")
        lay.addWidget(self.save)
        lay.addWidget(self.r_each)
        self.folder = PathRow(self.src.parent / f"{self.src.stem}_pages", "dir")
        lay.addWidget(self.folder)

        srow = QHBoxLayout()
        self.shrink = QCheckBox("Also reduce file size")
        self.shrink_btn = QPushButton("Options…")
        self.shrink_info = QLabel()
        self.shrink_info.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        srow.addWidget(self.shrink)
        srow.addWidget(self.shrink_btn)
        srow.addWidget(self.shrink_info, 1)
        lay.addSpacing(6)
        lay.addLayout(srow)
        self.shrink.toggled.connect(self._shrink_toggled)
        self.shrink_btn.clicked.connect(self._shrink_options)
        self.shrink_btn.setEnabled(False)
        self.shrink_info.setVisible(False)
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
        self.resize(560, self.sizeHint().height())
        self._update()

    def _chosen(self):
        """Entries for the pages typed in the box (raises RangeError)."""
        return [self.entries[i] for i in engine.parse_pages(self.pages.text(), self.count)]

    def _shrink_toggled(self, on):
        self.shrink_btn.setEnabled(on)
        if on and self.cvalues is None and not self._shrink_options():
            self.shrink.blockSignals(True)
            self.shrink.setChecked(False)
            self.shrink.blockSignals(False)
            self.shrink_btn.setEnabled(False)
        self.shrink_info.setVisible(self.shrink.isChecked())

    def _shrink_options(self):
        """Open the Reduce size choices for the pages being extracted. Returns True if confirmed."""
        try:
            pages = self._chosen()
        except engine.RangeError as e:
            QMessageBox.warning(self, "Extract pages", str(e))
            return False
        d = CompressOptionsDialog(self, self.src, self.password, pages, self.saved.get("compress"))
        if d.exec() != QDialog.DialogCode.Accepted:
            return False
        self.cvalues = d.opts.options()
        self.saved["compress"] = d.opts.remember()
        self.shrink_info.setText(d.opts.describe())
        self._info_pages = self.pages.text()
        return True

    def _update(self, *_):
        one = self.r_one.isChecked()
        self.save.setEnabled(one)
        self.folder.setEnabled(not one)
        if self.cvalues is not None and self.pages.text() != getattr(self, "_info_pages", None):
            self.shrink_info.setText(self.shrink_info.text().split(" · ")[0])   # estimate was for other pages
        try:
            positions = engine.parse_pages(self.pages.text(), self.count)
        except engine.RangeError as e:
            self.preview.setText(f"<span style='color:#d33'>{e}</span>")
            self.ok.setEnabled(False)
            return
        self.ok.setEnabled(True)
        n = len(positions)
        self.preview.setText(f"→ {n} page{'s' if n != 1 else ''}" +
                             (" in one PDF" if one else f" in {n} file{'s' if n != 1 else ''}"))
        if not self._name_edited:
            label = engine.format_ranges(positions).replace(", ", "_").replace(" ", "")
            if len(label) > 40:
                label = label[:40] + "_etc"
            self.save.edit.setText(str(self.src.parent / f"{self.src.stem}_pages_{label}.pdf"))

    def _accept(self):
        try:
            self.result_pages = self._chosen()
            self.result_one = self.r_one.isChecked()
            self.saved["extract_each"] = not self.result_one
            self.result_compress = self.cvalues if self.shrink.isChecked() else None
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


class CompressOptions(QWidget):
    """Preset / custom choice and options, with a live size estimate next to each choice.

    pages=None estimates compressing the whole file; a list of pages estimates extracting those pages.
    """

    KEYS = list(engine.PRESETS) + ["custom"]

    def __init__(self, src, password=None, pages=None, saved=None, parent=None):
        super().__init__(parent)
        self.src, self.password, self.pages = Path(src), password, pages
        self.est, self.futs, self.gen = {}, {}, {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setHorizontalSpacing(14)
        self.grp = QButtonGroup(self)
        self.radios, self.labels = {}, {}
        for row, (key, pr) in enumerate(engine.PRESETS.items()):
            rb = QRadioButton(f"{pr['label']}   ({pr['dpi']} dpi, quality {pr['quality']})")
            grid.addWidget(rb, row, 0, 1, 2)
            self.radios[key] = rb
        row = len(engine.PRESETS)
        rb = QRadioButton("Custom")
        self.radios["custom"] = rb
        grid.addWidget(rb, row, 0)
        crow = QHBoxLayout()
        self.dpi = QSpinBox()
        self.dpi.setRange(36, 600)
        self.dpi.setSuffix(" dpi")
        self.quality = QSpinBox()
        self.quality.setRange(10, 100)
        self.quality.setPrefix("quality ")
        crow.addWidget(self.dpi)
        crow.addWidget(self.quality)
        crow.addStretch(1)
        grid.addLayout(crow, row, 1)
        for i, key in enumerate(self.KEYS):
            self.grp.addButton(self.radios[key])
            lab = QLabel()
            lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            lab.setMinimumWidth(150)
            grid.addWidget(lab, i, 2)
            self.labels[key] = lab
        lay.addLayout(grid)
        lay.addWidget(_note("Pictures and scans larger than the chosen resolution are scaled down and re-saved as "
                            "JPEG. Text and vector graphics stay sharp. Fonts are trimmed to the characters used "
                            "and unused data is removed."))

        self.gray = QCheckBox("Convert images to black && white (grayscale)")
        self.meta = QCheckBox("Remove document properties (title, author, …)")
        self.no_img = QCheckBox("Don't touch images (lossless only)")
        for c in (self.gray, self.meta, self.no_img):
            lay.addWidget(c)
        self.summary = QLabel()
        self.summary.setStyleSheet("font-weight: 600; padding-top: 6px;")
        lay.addWidget(self.summary)

        c = saved or {}
        self.radios.get(c.get("preset", "balanced"), self.radios["balanced"]).setChecked(True)
        self.dpi.setValue(c.get("dpi", 150))
        self.quality.setValue(c.get("quality", 72))
        self.gray.setChecked(c.get("gray", False))
        self.meta.setChecked(c.get("meta", False))
        self.no_img.setChecked(c.get("no_img", False))

        self.custom_timer = QTimer(self)
        self.custom_timer.setSingleShot(True)
        self.custom_timer.setInterval(500)
        self.custom_timer.timeout.connect(lambda: self._estimate(["custom"]))
        for sb in (self.dpi, self.quality):
            sb.valueChanged.connect(self._custom_changed)
        for cb in (self.gray, self.meta, self.no_img):
            cb.toggled.connect(self._options_changed)
        self.grp.buttonToggled.connect(lambda *_: self._show_summary())
        self._enable()
        QTimer.singleShot(0, self._estimate_all)

    # ---------------------------------------------------------------- values
    def preset(self):
        return next(k for k, rb in self.radios.items() if rb.isChecked())

    def _key(self):
        return "lossless" if self.no_img.isChecked() else self.preset()

    def _opts(self, key):
        return engine.options_for("balanced" if key == "lossless" else key, self.dpi.value(), self.quality.value(),
                                  self.gray.isChecked(), self.meta.isChecked(), images=key != "lossless")

    def options(self):
        return self._opts(self._key())

    def remember(self):
        return {"preset": self.preset(), "dpi": self.dpi.value(), "quality": self.quality.value(),
                "gray": self.gray.isChecked(), "meta": self.meta.isChecked(), "no_img": self.no_img.isChecked()}

    def describe(self):
        key = self._key()
        name = "Lossless" if key == "lossless" else "Custom" if key == "custom" else \
            engine.PRESETS[key]["label"].split(" –")[0]
        r = self.est.get(key)
        return f"{name} · ≈ {human_size(r['size'])}" if r else name

    # ---------------------------------------------------------------- estimates
    def _enable(self):
        on = not self.no_img.isChecked()
        for w in list(self.radios.values()) + [self.dpi, self.quality, self.gray] + list(self.labels.values()):
            w.setEnabled(on)

    def _custom_changed(self, *_):
        self.radios["custom"].setChecked(True)
        self.labels["custom"].setText("…")
        self.est.pop("custom", None)
        self._show_summary()
        self.custom_timer.start()

    def _options_changed(self, *_):
        self._enable()
        self._estimate_all()

    def _estimate_all(self):
        self.est.clear()
        self._estimate(["lossless"] if self.no_img.isChecked() else self.KEYS)
        if self.no_img.isChecked():
            for lab in self.labels.values():
                lab.setText("")

    def _estimate(self, keys):
        for key in keys:
            old = self.futs.pop(key, None)
            if old is not None:
                old.cancel()
            g = self.gen[key] = self.gen.get(key, 0) + 1
            if key in self.labels:
                self.labels[key].setText("estimating…")
            self.futs[key] = bridge().submit(
                engine.estimate, str(self.src), self._opts(key), self.pages, self.password,
                on_done=lambda r, k=key, g=g: self._got(k, g, r),
                on_error=lambda m, k=key, g=g: self._got(k, g, None))
        self._show_summary()

    def _got(self, key, g, r):
        if self.gen.get(key) != g:
            return   # a newer estimate for this choice is on its way
        self.futs.pop(key, None)
        if r is None:
            if key in self.labels:
                self.labels[key].setText("—")
            return
        self.est[key] = r
        if key in self.labels:
            self.labels[key].setText(self._fmt(r))
            self.labels[key].setToolTip("" if r["exact"] else "Estimated from a sample of pages")
        self._show_summary()

    @staticmethod
    def _fmt(r):
        if r["size"] >= r["base"]:
            return "no smaller"
        pct = 100 * (1 - r["size"] / r["base"])
        return f"≈ {human_size(r['size'])}  (−{pct:.0f}%)"

    def _show_summary(self):
        r = self.est.get(self._key())
        what = "Estimated size" if self.pages is None else "Estimated size of the extracted file"
        if r is None:
            self.summary.setText(f"{what}: estimating…")
        elif r["size"] >= r["base"]:
            self.summary.setText(f"{what}: can't get smaller than {human_size(r['base'])} with these settings")
        else:
            self.summary.setText(f"{what}: ≈ {human_size(r['size'])}  "
                                 f"(from {human_size(r['base'])}, {100 * (1 - r['size'] / r['base']):.0f}% smaller)")

    def cancel(self):
        for f in self.futs.values():
            f.cancel()
        self.futs.clear()
        self.gen = {k: v + 1 for k, v in self.gen.items()}


class CompressDialog(QDialog):
    def __init__(self, parent, src, password, settings, pages=None):
        super().__init__(parent)
        self.setWindowTitle("Reduce file size")
        self.src = Path(src)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{self.src.name}</b> — currently {human_size(self.src.stat().st_size)}"))
        if pages is not None:
            lay.addWidget(_note("Includes your Organize changes (page order, rotation and deletions)."))
        self.opts = CompressOptions(src, password, pages, settings.get("compress"))
        lay.addWidget(self.opts)

        form = QFormLayout()
        self.out = PathRow(self.src.parent / f"{self.src.stem}_compressed.pdf", "save")
        form.addRow("Save as:", self.out)
        lay.addLayout(form)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        bb.addButton("Compress", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.finished.connect(lambda _: self.opts.cancel())
        self.resize(620, self.sizeHint().height())

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


class CompressOptionsDialog(QDialog):
    """The Reduce size choices for pages being extracted (opened from the Extract window)."""

    def __init__(self, parent, src, password, pages, saved):
        super().__init__(parent)
        self.setWindowTitle("Reduce size of extracted pages")
        lay = QVBoxLayout(self)
        n = len(pages)
        lay.addWidget(QLabel(f"<b>{Path(src).name}</b> — {n} page{'s' if n != 1 else ''} to extract"))
        self.opts = CompressOptions(src, password, pages, saved)
        lay.addWidget(self.opts)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.finished.connect(lambda _: self.opts.cancel())
        self.resize(620, self.sizeHint().height())


def _self_command(*args):
    """Command line that starts this app again (the .exe when built, run.py from source)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "run.py"), *args]


def qr_pixmap(text, size=240):
    import qrcode
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    m = qr.get_matrix()
    n = len(m)
    cell = max(1, size // n)
    pm = QPixmap(n * cell, n * cell)
    pm.fill(Qt.GlobalColor.white)
    p = QPainter(pm)
    for y, row in enumerate(m):
        for x, on in enumerate(row):
            if on:
                p.fillRect(x * cell, y * cell, cell, cell, Qt.GlobalColor.black)
    p.end()
    return pm


class WifiDialog(QDialog):
    """Shows how to open the tools on a phone: a QR code and the addresses, the on/off switch, how many PDFs
    people opened, and a reminder while their uploads are stored on this computer."""
    turnOff = Signal()
    turnOn = Signal()

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Share via Wi-Fi")
        self.setModal(False)
        lay = QVBoxLayout(self)
        self.head = QLabel()
        self.head.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.head.setWordWrap(True)
        lay.addWidget(self.head)
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr.setMinimumSize(250, 250)
        lay.addWidget(self.qr)
        self.links = QWidget()            # one row per address: the link and a Copy button
        self.links_lay = QVBoxLayout(self.links)
        self.links_lay.setContentsMargins(0, 4, 0, 0)
        self.links_lay.setSpacing(6)
        lay.addWidget(self.links)
        self.mdns = _note("")
        self.mdns.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.mdns)
        tip = ("<b>Phone can't connect?</b> The first time, macOS asks whether to accept incoming connections for "
               "dl_pdf_tools: click <i>Allow</i>." if sys.platform == "darwin" else
               "<b>Phone can't connect?</b> When Windows asks, allow dl_pdf_tools on <i>Private networks</i>, and "
               "make sure this Wi-Fi is set to <i>Private</i> in Windows Settings → Network &amp; internet.")
        self.note = _note(
            "Phones, tablets and laptops on the same Wi-Fi as this computer can open one of these addresses (or "
            "scan the code with the phone's camera). They open a PDF; this computer splits it, extracts pages or "
            "reduces its size, and they download the result. Each device only sees its own files.<br><br>"
            "Keep this computer on with dl_pdf_tools open. There's no password: anyone on this Wi-Fi who knows "
            "the address can use it while sharing is on. Their PDFs are kept on this computer until you delete "
            "them (Admin…).<br><br>" + tip)
        self.note.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.note)
        self.count = _note("")
        lay.addWidget(self.count)
        self.store_row = QWidget()
        sl = QVBoxLayout(self.store_row)
        sl.setContentsMargins(0, 4, 0, 0)
        self.store_lbl = QLabel()
        self.store_lbl.setWordWrap(True)
        self.store_lbl.setStyleSheet(f"color: {WARN_COLOR};")
        sl.addWidget(self.store_lbl)
        sr = QHBoxLayout()
        b = QPushButton("Open folder")
        b.clicked.connect(win.wifi_open_folder)
        sr.addWidget(b)
        b = QPushButton("Delete all…")
        b.clicked.connect(lambda: win.wifi_delete_all(self))
        sr.addWidget(b)
        sr.addStretch(1)
        sl.addLayout(sr)
        self.store_row.hide()
        lay.addWidget(self.store_row)
        row = QHBoxLayout()
        self.toggle = QPushButton("Turn off")
        self.toggle.setMinimumWidth(110)
        self.toggle.clicked.connect(self._toggle)
        row.addWidget(self.toggle)
        row.addStretch(1)
        b = QPushButton("Admin…")
        b.clicked.connect(win.wifi_admin)
        row.addWidget(b)
        b = QPushButton("Close")
        b.clicked.connect(self.close)
        row.addWidget(b)
        lay.addLayout(row)
        self.is_on = False
        self.setFixedWidth(480)

    def _toggle(self):
        (self.turnOff if self.is_on else self.turnOn).emit()

    def starting(self):
        self.is_on = True
        self.head.setText("Starting Wi-Fi sharing…")
        self.qr.clear()
        self.qr.setText("…")
        self._set_links([])
        self.mdns.clear()
        self.count.clear()
        self.toggle.setText("Turn off")

    def ready(self, st):
        urls = st["urls"]
        self.is_on = True
        self.head.setText("Sharing is on. Open dl_pdf_tools on your phone:")
        self.qr.setPixmap(qr_pixmap(urls[0]))
        self._set_links(urls)
        self.mdns.setText({"pending": "(the .local address appears in a moment)",
                           "off": "(this network blocks .local names: use the number address)"}.get(st.get("mdns"), ""))
        self.mdns.setVisible(bool(self.mdns.text()))
        self.toggle.setText("Turn off")
        self.counts(st)
        self.adjustSize()

    def _set_links(self, urls):
        """The addresses, each with a Copy button (rebuilt only when they change)."""
        if getattr(self, "_urls", None) == list(urls):
            return
        self._urls = list(urls)
        while self.links_lay.count():
            w = self.links_lay.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        for u in urls:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            lab = QLabel(f'<a href="{u}" style="text-decoration: none;">{u}</a>')
            lab.setStyleSheet("font-size: 16px; font-weight: 600;")
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            lab.setOpenExternalLinks(True)
            lab.setToolTip("Open in your browser")
            h.addWidget(lab, 1)
            b = QPushButton("Copy")
            b.setAutoDefault(False)
            b.setMinimumWidth(90)
            b.setToolTip(f"Copy {u}")
            b.clicked.connect(lambda _=False, u=u, b=b: self._copy(u, b))
            h.addWidget(b)
            self.links_lay.addWidget(row)

    def _copy(self, url, button):
        QApplication.clipboard().setText(url)
        button.setText("Copied ✓")
        QTimer.singleShot(1500, button, lambda: button.setText("Copy"))   # skipped if the row is gone

    def counts(self, st):
        n, d = st.get("files", 0), st.get("devices", 0)
        self.count.setText(f"{n} PDF{'s' * (n != 1)} opened from {d} device{'s' * (d != 1)} since sharing started."
                           if n else "No PDFs opened yet.")

    def off(self, error=None):
        self.is_on = False
        self.head.setText(f"Couldn't start sharing: {error}" if error else "Wi-Fi sharing is off.")
        self.qr.clear()
        self.qr.setText("Off")
        self._set_links([])
        self.mdns.hide()
        self.count.clear()
        self.toggle.setText("Turn on")

    def stored(self, n, size):
        """Reminder: uploads from Wi-Fi users still on this computer."""
        if n:
            self.store_lbl.setText(f"⚠  {n} upload{'s' * (n != 1)} ({human_size(size)}) stored on this computer. "
                                   "Delete them when you no longer need them.")
        if self.store_row.isVisible() != bool(n):
            self.store_row.setVisible(bool(n))
            self.adjustSize()


class WifiAdminDialog(QDialog):
    """The uploads people left on this computer (open the folder / delete all), the activity log, and
    blocking devices."""
    COLS = wifi_store.COLS
    WIDTHS = (88, 70, 110, 150, 130, 200, 170, 360)

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Wi-Fi admin")
        self.setModal(False)
        self.resize(1180, 640)
        lay = QVBoxLayout(self)
        t = QLabel("Stored uploads")
        t.setStyleSheet("font-size: 15px; font-weight: 600;")
        lay.addWidget(t)
        self.warn = QLabel()
        self.warn.setWordWrap(True)
        lay.addWidget(self.warn)
        r = QHBoxLayout()
        where = _note(str(wifi_store.WORK))
        where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        r.addWidget(where, 1)
        b = QPushButton("Open folder")
        b.clicked.connect(win.wifi_open_folder)
        r.addWidget(b)
        b = QPushButton("Delete all uploads…")
        b.clicked.connect(self._delete)
        r.addWidget(b)
        lay.addLayout(r)
        lay.addWidget(_note(
            "Each PDF is kept in its own folder named by date, time and the sender's IP address (its results are in "
            "the \"out-…\" folders next to it), even after sharing is turned off or the app is closed, until you "
            "press Delete all."))
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet("background: palette(mid);")
        lay.addSpacing(6)
        lay.addWidget(line)
        lay.addSpacing(6)
        hr = QHBoxLayout()
        t = QLabel("Activity log")
        t.setStyleSheet("font-size: 15px; font-weight: 600;")
        hr.addWidget(t)
        hr.addStretch(1)
        hr.addWidget(QLabel("Filter"))
        self.filter = QLineEdit()
        self.filter.setFixedWidth(220)
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(lambda: self._fill(force=True))
        hr.addWidget(self.filter)
        b = QPushButton("Open log (Excel / CSV)")
        b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(wifi_store.LOG_CSV)))
                          if wifi_store.LOG_CSV.exists() else None)
        hr.addWidget(b)
        self.block_btn = QPushButton("Block device…")
        self.block_btn.setMinimumWidth(130)
        self.block_btn.setEnabled(False)
        self.block_btn.clicked.connect(self._block)
        hr.addWidget(self.block_btn)
        b = QPushButton("Clear log…")
        b.clicked.connect(self._clear)
        hr.addWidget(b)
        lay.addLayout(hr)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.setWordWrap(False)
        for i, w in enumerate(self.WIDTHS):
            self.table.setColumnWidth(i, w)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._sel_changed)
        lay.addWidget(self.table, 1)
        br = QHBoxLayout()
        self.blocked_lbl = QLabel()
        self.blocked_lbl.setWordWrap(True)
        br.addWidget(self.blocked_lbl, 1)
        b = QPushButton("Unblock all")
        b.clicked.connect(self._unblock_all)
        br.addWidget(b)
        lay.addLayout(br)
        copy_key = "Cmd+C" if sys.platform == "darwin" else "Ctrl+C"
        lay.addWidget(_note(
            "Device name and MAC address are looked up on your network and may be blank (many phones don't share a "
            "name, and use a private MAC per Wi-Fi). Location: everyone here is on your own Wi-Fi, and local "
            f"addresses like 192.168.x.x can't be placed on a map. Select rows and press {copy_key} to copy them. "
            "Block device: select one of its rows. Its page closes within a few seconds and it can't come back "
            "(by IP address; a device that gets a new address could)."))
        cb = QHBoxLayout()
        cb.addStretch(1)
        b = QPushButton("Close")
        b.clicked.connect(self.close)
        cb.addWidget(b)
        lay.addLayout(cb)
        copy = QAction(self)
        copy.setShortcut(QKeySequence.StandardKey.Copy)
        copy.triggered.connect(self._copy)
        self.table.addAction(copy)
        self._sig = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._tick()

    def showEvent(self, e):
        super().showEvent(e)
        self.timer.start(2000)
        self._tick()

    def _delete(self):
        if self.win.wifi_delete_all(self):
            self._tick()

    def _clear(self):
        if QMessageBox.question(self, "Clear the activity log?",
                                "Delete every line of the Wi-Fi activity log?") == QMessageBox.StandardButton.Yes:
            wifi_store.clear_log()
            self._fill(force=True)

    # ---------------------------------------------------------------- block / unblock
    def _sel_row(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            return None, None
        vals = [(self.table.item(rows[0], c).text() if self.table.item(rows[0], c) else "")
                for c in range(len(self.COLS))]
        return (vals[2], vals) if vals[2] else (None, None)

    def _sel_changed(self):
        ip, _r = self._sel_row()
        if not ip:
            self.block_btn.setText("Block device…")
            self.block_btn.setEnabled(False)
        else:
            self.block_btn.setText("Unblock device" if wifi_store.is_blocked(ip) else "Block device…")
            self.block_btn.setEnabled(True)

    def _block(self):
        ip, r = self._sel_row()
        if not ip:
            return
        if wifi_store.is_blocked(ip):
            wifi_store.unblock(ip)
        else:
            who = " · ".join(x for x in (r[3], r[5]) if x) or "this device"
            if QMessageBox.question(self, "Block this device?", (
                    f"Block {ip} ({who})?\n\nIts {APP_NAME} page closes within a few seconds and shows "
                    "“Access ended”. It can't use sharing again until you unblock it. Its uploads stay in the "
                    "folder.")) != QMessageBox.StandardButton.Yes:
                return
            wifi_store.block(ip, r[5], r[3])
            self.win.wifi_command("kick", ip=ip)
        self._sel_changed()
        self._show_blocked()
        QTimer.singleShot(1800, lambda: self._fill(force=True))

    def _unblock_all(self):
        for ip in list(wifi_store.blocked()):
            wifi_store.unblock(ip)
        self._sel_changed()
        self._show_blocked()
        QTimer.singleShot(1800, lambda: self._fill(force=True))

    def _show_blocked(self):
        b = wifi_store.blocked()
        txt = "Blocked devices: " + ("none" if not b else "   ".join(
            f"⛔ {ip}" + (f" ({v.get('device')})" if v.get("device") else "") + f" since {v.get('since', '')}"
            for ip, v in b.items()))
        if self.blocked_lbl.text() != txt:
            self.blocked_lbl.setText(txt)

    def _copy(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if rows:
            QApplication.clipboard().setText("\n".join(
                "\t".join(self.table.item(r, c).text() if self.table.item(r, c) else "" for c in range(len(self.COLS)))
                for r in rows))

    def _fill(self, force=False):
        rows = wifi_store.read_log()
        q = self.filter.text().strip().lower()
        if q:
            rows = [r for r in rows if q in " ".join(r).lower()]
        sig = (len(rows), tuple(rows[0]) if rows else None, q)
        if sig == self._sig and not force:
            return
        self._sig = sig
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for c, v in enumerate((r + [""] * len(self.COLS))[:len(self.COLS)]):
                it = QTableWidgetItem(v)
                if c == len(self.COLS) - 1:
                    it.setToolTip(v)
                self.table.setItem(i, c, it)
        self._sel_changed()

    def _tick(self):
        n, size = wifi_store.stats()
        if n:
            self.warn.setText(f"⚠  {n} upload{'s' * (n != 1)} from Wi-Fi users stored on this computer "
                              f"({human_size(size)}). Delete them when you no longer need them.")
            self.warn.setStyleSheet(f"color: {WARN_COLOR};")
        else:
            self.warn.setText("No uploads stored.")
            self.warn.setStyleSheet("")
        self._fill()
        self._show_blocked()

    def closeEvent(self, e):
        self.timer.stop()
        super().closeEvent(e)


# ======================================================================== main window

class OrganizeView(QWidget):
    """Overview of every page as a grid: drag to reorder, rotate, delete, extract, then save as a new PDF.

    Changes only live here until "Save as new PDF"; the original file is never touched.
    """
    changed = Signal()
    extractRequested = Signal(list)   # selected positions (0-based) in the current arrangement
    splitRequested = Signal(str)      # ranges text, e.g. "1-3, 4-8"
    saveRequested = Signal()

    TILE = 150
    ROLE = Qt.ItemDataRole.UserRole

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc, self.sizes, self.base = None, [], {}
        self.history, self.saved = [], []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 6)

        bar = QHBoxLayout()

        def btn(text, slot, tip=None):
            b = QPushButton(text)
            b.clicked.connect(slot)
            if tip:
                b.setToolTip(tip)
            bar.addWidget(b)
            return b

        self.b_left = btn("⟲ Rotate left", lambda: self.rotate(-90), "Rotate the selected pages left (L)")
        self.b_right = btn("⟳ Rotate right", lambda: self.rotate(90), "Rotate the selected pages right (R)")
        self.b_back = btn("◀ Move", lambda: self.move(-1), "Move the selected pages earlier (Ctrl+←)")
        self.b_fwd = btn("Move ▶", lambda: self.move(1), "Move the selected pages later (Ctrl+→)")
        self.b_delete = btn("🗑 Delete", self.delete_selected, "Delete the selected pages (Del)")
        self.b_extract = btn("Extract selected…", lambda: self.extractRequested.emit(self.selected_positions()),
                             "Save the selected pages as new PDF(s)")
        bar.addSpacing(10)
        self.b_undo = btn("Undo", self.undo, "Undo the last change (Ctrl+Z)")
        self.b_reset = btn("Reset", self.reset, "Undo all changes")
        bar.addStretch(1)
        self.info = QLabel()
        bar.addWidget(self.info)
        self.b_save = btn("Save as new PDF…", self.saveRequested.emit, "Write the pages as they are here to a new PDF")
        self.b_save.setStyleSheet("font-weight: 600;")
        lay.addLayout(bar)
        lay.addWidget(_note("Drag pages to change their order. Click to select; Ctrl+click or Shift+click to select "
                            "several. Right-click for more. Your original file isn't changed: "
                            "<b>Save as new PDF</b> writes a copy."))

        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.ViewMode.ListMode)   # ListMode + wrapping = a grid that reorders
        self.grid.setFlow(QListWidget.Flow.LeftToRight)
        self.grid.setWrapping(True)
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(12)
        self.grid.setIconSize(QSize(self.TILE, int(self.TILE * 1.42) + 24))
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.grid.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._menu)
        self.grid.itemSelectionChanged.connect(self._update_info)
        self.grid.model().rowsMoved.connect(lambda *_: self._after_drag())
        lay.addWidget(self.grid, 1)

        for keys, slot in (("Del", self.delete_selected), ("Ctrl+Z", self.undo), ("L", lambda: self.rotate(-90)),
                           ("R", lambda: self.rotate(90)), ("Ctrl+Left", lambda: self.move(-1)),
                           ("Ctrl+Right", lambda: self.move(1))):
            a = QAction(self)
            a.setShortcut(QKeySequence(keys))
            a.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            a.triggered.connect(slot)
            self.addAction(a)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._render_some)
        self._drag_snapshot = None
        self.grid.viewport().installEventFilter(self)

    # ---------------------------------------------------------------- state
    def set_doc(self, doc):
        self.timer.stop()
        self.doc, self.base = doc, {}
        self.sizes = [(p.rect.width, p.rect.height) for p in doc] if doc else []
        self.saved = [(i, 0) for i in range(len(self.sizes))]
        self.history = []
        self._fill(self.saved)
        if doc and self.isVisible():
            self.timer.start(0)

    def entries(self):
        return [tuple(self.grid.item(i).data(self.ROLE)) for i in range(self.grid.count())]

    def is_dirty(self):
        return self.doc is not None and self.entries() != self.saved

    def mark_saved(self):
        self.saved = self.entries()
        self._update_info()

    def selected_positions(self):
        return sorted(self.grid.row(it) for it in self.grid.selectedItems())

    def _fill(self, entries, select=()):
        self.grid.clear()
        for e in entries:
            it = QListWidgetItem()
            it.setData(self.ROLE, list(e))
            it.setSizeHint(QSize(self.TILE + 16, int(self.TILE * 1.42) + 34))
            self.grid.addItem(it)
        for r in select:
            if 0 <= r < self.grid.count():
                self.grid.item(r).setSelected(True)
        self._refresh()

    def _push(self):
        self.history.append(self.entries())
        del self.history[:-100]

    def _changed(self):
        self._refresh()
        self.changed.emit()

    # ---------------------------------------------------------------- actions
    def rotate(self, delta):
        items = self.grid.selectedItems()
        if not items:
            return
        self._push()
        for it in items:
            p, rot = it.data(self.ROLE)
            it.setData(self.ROLE, [p, (rot + delta) % 360])
        self._changed()

    def delete_selected(self):
        rows = self.selected_positions()
        if not rows:
            return
        if len(rows) == self.grid.count():
            QMessageBox.information(self, "Organize", "A PDF needs at least one page, so you can't delete them all.")
            return
        self._push()
        for r in reversed(rows):
            self.grid.takeItem(r)
        self._changed()

    def move(self, direction):
        rows = self.selected_positions()
        if not rows:
            return
        ents = self.entries()
        if (direction < 0 and rows[0] == 0) or (direction > 0 and rows[-1] == len(ents) - 1):
            return
        self._push()
        chosen = set(rows)
        moving = [ents[r] for r in rows]                     # selected pages move together as one block
        rest = [e for i, e in enumerate(ents) if i not in chosen]
        if direction < 0:
            at = rows[0] - 1                                  # one place before the first selected page
        else:
            at = rows[-1] + 1 - len(rows) + 1                 # one place after the last selected page
        new = rest[:at] + moving + rest[at:]
        self._fill(new, range(at, at + len(moving)))
        self.changed.emit()

    def undo(self):
        if self.history:
            self._fill(self.history.pop())
            self.changed.emit()

    def reset(self):
        if self.is_dirty():
            self._push()
            self._fill(self.saved)
            self.changed.emit()

    # Drag and drop: remember the order before the drag so it can be undone.
    def eventFilter(self, obj, e):
        if e.type() == e.Type.MouseButtonPress:
            self._drag_snapshot = self.entries()
        return False

    def _after_drag(self):
        if self._drag_snapshot is not None and self._drag_snapshot != self.entries():
            self.history.append(self._drag_snapshot)
        self._drag_snapshot = None
        QTimer.singleShot(0, self._changed)

    def _menu(self, pos):
        it = self.grid.itemAt(pos)
        if it is not None and not it.isSelected():
            self.grid.clearSelection()
            it.setSelected(True)
        n = len(self.grid.selectedItems())
        m = QMenu(self)
        if n:
            m.addAction("Rotate left", lambda: self.rotate(-90))
            m.addAction("Rotate right", lambda: self.rotate(90))
            m.addAction(f"Delete {n} page{'s' if n > 1 else ''}", self.delete_selected)
            m.addSeparator()
            m.addAction(f"Extract {n} page{'s' if n > 1 else ''}…",
                        lambda: self.extractRequested.emit(self.selected_positions()))
        if it is not None:
            row, count = self.grid.row(it), self.grid.count()
            if row < count - 1:
                m.addAction(f"Split after this page ({row + 1})…",
                            lambda: self.splitRequested.emit(f"1-{row + 1}, {row + 2}-{count}"))
        m.addSeparator()
        m.addAction("Select all", self.grid.selectAll)
        m.exec(self.grid.viewport().mapToGlobal(pos))

    # ---------------------------------------------------------------- drawing
    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() in (e.Type.PaletteChange, e.Type.StyleChange) and self.doc:
            QTimer.singleShot(0, self._refresh)

    def showEvent(self, e):
        super().showEvent(e)
        if self.doc and len(self.base) < len(self.sizes):
            self.timer.start(0)

    def _render_some(self):
        if not self.doc:
            self.timer.stop()
            return
        top = self.grid.indexAt(self.grid.viewport().rect().topLeft() + QPoint(20, 20)).row()
        wanted = [self.grid.item(r).data(self.ROLE)[0] for r in range(max(0, top), min(self.grid.count(), top + 24))]
        todo = [p for p in wanted if p not in self.base][:3]
        if not todo:
            todo = [p for p in range(len(self.sizes)) if p not in self.base][:3]
        if not todo:
            self.timer.stop()
            return
        dpr = self.devicePixelRatioF()
        long_side = int(self.TILE * 1.42)
        for p in todo:
            w, h = self.sizes[p]
            try:
                self.base[p] = render(self.doc[p], long_side / max(w, h), dpr)
            except Exception:
                self.base[p] = QImage()
        for r in range(self.grid.count()):
            if self.grid.item(r).data(self.ROLE)[0] in todo:
                self._paint_item(r)

    def _refresh(self):
        for r in range(self.grid.count()):
            self._paint_item(r)
        self._update_info()

    def _paint_item(self, row):
        it = self.grid.item(row)
        p, rot = it.data(self.ROLE)
        dpr = self.devicePixelRatioF()
        W, H, CAP = self.TILE, int(self.TILE * 1.42), 24
        pm = QPixmap(int(W * dpr), int((H + CAP) * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w, h = self.sizes[p]
        if rot in (90, 270):
            w, h = h, w
        s = min(W / w, H / h)
        tw, th = w * s, h * s
        target = QRectF((W - tw) / 2, (H - th) / 2, tw, th)
        base = self.base.get(p)
        painter.fillRect(target, Qt.GlobalColor.white)
        if base is not None and not base.isNull():
            img = base.transformed(QTransform().rotate(rot)) if rot else base
            painter.drawImage(target, img)
        painter.setPen(QColor(150, 150, 150))
        painter.drawRect(target)
        painter.setPen(self.palette().color(self.foregroundRole()))
        cap = f"{row + 1}"
        if p != row:
            cap += f"   (p. {p + 1})"
        if rot:
            cap += "  ↻"
        painter.drawText(QRectF(0, H + 4, W, CAP - 4), int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                         cap)
        painter.end()
        it.setIcon(QIcon(pm))
        it.setToolTip(f"Page {row + 1}" + (f", originally page {p + 1}" if p != row else "") +
                      (f", rotated {rot}°" if rot else ""))

    def _update_info(self):
        n, sel = self.grid.count(), len(self.grid.selectedItems())
        text = f"{n} page{'s' if n != 1 else ''}"
        if sel:
            text += f" · {sel} selected"
        dirty = self.is_dirty()
        if dirty:
            text += " · <b>unsaved changes</b>"
        self.info.setText(text)
        for b in (self.b_left, self.b_right, self.b_back, self.b_fwd, self.b_delete, self.b_extract):
            b.setEnabled(sel > 0)
        self.b_undo.setEnabled(bool(self.history))
        self.b_reset.setEnabled(dirty)
        self.b_save.setEnabled(self.doc is not None)


class _Worker(QThread):
    """Runs fn(report) on a thread (network only, never MuPDF); report(done, total) for progress."""
    progress = Signal(object, object)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn(lambda d, t: self.progress.emit(d, t)))
        except Exception as e:
            self.failed.emit(str(e) or e.__class__.__name__)


class UpdateDialog(QDialog):
    """Check for updates: this version, the latest on GitHub, what's new, and Update now / Check again /
    Release page."""

    def __init__(self, parent, settings, info=None):
        super().__init__(parent)
        self.setWindowTitle("Check for updates")
        self.settings, self.info, self.worker = settings, info, None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(8)
        self.title = QLabel(f"{APP_NAME} {APP_VERSION}  (this version)")
        self.title.setStyleSheet("font-size: 15px; font-weight: 600;")
        lay.addWidget(self.title)
        lay.addSpacing(4)
        self.head = QLabel()
        self.head.setWordWrap(True)
        lay.addWidget(self.head)
        self.sub = QLabel()
        self.sub.setWordWrap(True)
        self.sub.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        lay.addWidget(self.sub)
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        self.notes.setMinimumHeight(240)
        lay.addWidget(self.notes, 1)
        self.auto = QCheckBox("Check for updates automatically (once a day)")
        self.auto.setChecked(settings.get("auto_update", True))
        self.auto.toggled.connect(lambda on: self.settings.__setitem__("auto_update", on))
        lay.addWidget(self.auto)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.bar = QProgressBar()
        self._bar_idle()
        lay.addWidget(self.bar)
        row = QHBoxLayout()
        self.b_update = QPushButton("Update now")
        self.b_again = QPushButton("Check again")
        self.b_page = QPushButton("Release page")
        self.b_close = QPushButton("Close")
        for b in (self.b_update, self.b_again, self.b_page):
            b.setAutoDefault(False)
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.b_close)
        lay.addLayout(row)
        self.b_update.clicked.connect(self.start_update)
        self.b_again.clicked.connect(self.check)
        self.b_page.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(
            (self.info or {}).get("page") or updater.RELEASES_PAGE)))
        self.b_close.clicked.connect(self.reject)
        self.b_close.setDefault(True)
        self.resize(640, 560)
        if info:
            self._show(info)
        else:
            self.check()

    def _bar_idle(self, value=0):
        self.bar.setRange(0, 1000)
        self.bar.setValue(value)
        self.bar.setTextVisible(False)

    def _headline(self, text, kind=None):
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        colors = {"ok": ("#1a7f37", "#4cc76a"), "new": ("#0969da", "#58a6ff"), "bad": ("#cf222e", "#ff7b72")}
        color = f"color: {colors[kind][dark]};" if kind else ""
        self.head.setStyleSheet(f"font-size: 17px; font-weight: 600; {color}")
        self.head.setText(text)

    def _set_busy(self, busy):
        self.b_again.setEnabled(not busy)
        if busy:
            self.b_update.setEnabled(False)

    def check(self):
        if self.worker is not None and self.worker.isRunning():
            return
        self._headline("Checking for updates…")
        self.sub.setText("Asking GitHub for the latest version.")
        self.status.setText("")
        self.bar.setRange(0, 0)   # busy
        self._set_busy(True)
        self.worker = _Worker(lambda _r: updater.latest(), self)
        self.worker.done.connect(self._show)
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _show(self, info):
        self.info = info
        self.settings["last_update_check"] = time.time()
        self._bar_idle()
        self._set_busy(False)
        ver = info["version"]
        line = f"Latest on GitHub: {ver}"
        if info.get("date"):
            line += f", released {info['date']}"
        if info.get("token"):
            line += "  ·  checked with this PC's GitHub login (private repo)"
        self.sub.setText(line)
        notes = (info.get("notes") or "").strip() or "_No release notes._"
        self.notes.setMarkdown(f"**What's new in {ver}:**\n\n{notes}")
        if not updater.is_newer(ver):
            self._headline(f"You're on the latest version ({APP_VERSION}) ✔", "ok")
            self.status.setText("Everything is up to date.")
            self.b_update.setEnabled(False)
            return
        self._headline(f"Version {ver} is available", "new")
        ok, why = updater.can_install()
        if not info.get("asset"):
            ok, why = False, f"The new version has no download for this computer ({updater.asset_name()})."
        self.b_update.setEnabled(ok)
        if ok:
            size = info["asset"].get("size")
            self.status.setText(f"Update now downloads {human_size(size) if size else 'it'}, closes {APP_NAME}, "
                                f"installs {ver} and opens it again. Your settings are kept.")
        else:
            self.status.setText(f"{why} Use Release page to download it yourself.")

    def _failed(self, msg):
        self._bar_idle()
        self._set_busy(False)
        self._headline("Couldn't check for updates", "bad")
        self.sub.setText("")
        self.status.setText(msg)
        if not self.info:
            self.notes.clear()

    def start_update(self):
        info = self.info
        self._set_busy(True)
        self._headline(f"Downloading version {info['version']}…", "new")
        self.status.setText("Downloading…")
        self.bar.setRange(0, 0)
        tmp = tempfile.mkdtemp(prefix="dlpt_dl_")

        def work(report):
            pkg = updater.download(info, tmp, report)
            return updater.prepare_install(pkg, os.getpid())

        self.worker = _Worker(work, self)
        self.worker.progress.connect(self._progress)
        self.worker.done.connect(self._ready)
        self.worker.failed.connect(self._download_failed)
        self.worker.start()

    def _progress(self, done, total):
        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * done / total))
            self.bar.setTextVisible(True)
            self.bar.setFormat(f"{human_size(done)} of {human_size(total)}")
            self.status.setText(f"Downloading {human_size(done)} of {human_size(total)}…")

    def _download_failed(self, msg):
        self._bar_idle()
        self._set_busy(False)
        self.b_update.setEnabled(True)
        self._headline("The update didn't download", "bad")
        self.status.setText(msg)

    def _ready(self, prepared):
        cmd, _work = prepared
        self._bar_idle(1000)
        self._headline(f"Version {self.info['version']} is ready to install", "ok")
        self.status.setText("dl_pdf_tools will close, update itself and open again. This takes a few seconds.")
        for b in (self.b_update, self.b_again, self.b_page):
            b.setEnabled(False)
        save_settings(self.settings)
        updater.start_helper(cmd)   # it waits for this app to close before replacing anything
        QTimer.singleShot(1200, self._quit)

    def _quit(self):
        self.accept()
        win = self.parent()
        if win is not None and not win.close():
            # The user chose to stay (e.g. unsaved Organize changes); the update installs when the app closes.
            QMessageBox.information(win, "Update", "The update will be installed when you close dl_pdf_tools.")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.doc, self.path, self.password, self.busy = None, None, None, False
        self.wifi_proc, self.wifi_dlg, self.wifi_state = None, None, None
        self.wifi_admin_dlg, self._wifi_hidden = None, None
        self.wifi_timer = QTimer(self)
        self.wifi_timer.timeout.connect(self._wifi_poll)
        self.setAcceptDrops(True)
        ico = asset("icon.ico")
        if ico:
            self.setWindowIcon(QIcon(str(ico)))

        self.view = PdfView()
        self.organize = OrganizeView()
        self.organize.changed.connect(self._organize_changed)
        self.organize.extractRequested.connect(self.extract)
        self.organize.splitRequested.connect(self.split)
        self.organize.saveRequested.connect(self.save_organized)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.organize)
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(self._build_wifi_note())
        cl.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.empty = QLabel("Open a PDF  (Ctrl+O)  or drop one here", self.view)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("font-size: 15px;")
        self.empty.setForegroundRole(QPalette.ColorRole.PlaceholderText)

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
        self.wifi_label = QPushButton()
        self.wifi_label.setFlat(True)
        self.wifi_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.wifi_label.setToolTip("Show the QR code and addresses")
        self.wifi_label.clicked.connect(lambda: (self.wifi_dialog().show(), self.wifi_dialog().raise_()))
        self.wifi_label.hide()
        self.status.addPermanentWidget(self.wifi_label)
        self.update_label = QPushButton()
        self.update_label.setFlat(True)
        self.update_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_label.setStyleSheet("font-weight: 600;")
        self.update_label.clicked.connect(lambda: self.check_updates(self._update_info))
        self.update_label.hide()
        self.status.addPermanentWidget(self.update_label)
        self._update_info = None
        QTimer.singleShot(3000, self._auto_update_check)
        self.status.addPermanentWidget(self.info)

        g = self.settings.get("geometry")
        if g:
            self.restoreGeometry(bytes.fromhex(g))
        else:
            self.resize(1200, 860)
        self.dock.setVisible(self.settings.get("pages_panel", True))
        fit = self.settings.get("fit", "width")
        self.view.fit = fit if fit in ("width", "page") else None
        if self.view.fit is None:
            self.view.canvas.zoom = float(self.settings.get("zoom", 1.0))
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

        self.a_organize = self._act("Organize", self.toggle_organize, "Ctrl+G", SP.SP_FileDialogContentsView,
                                    "See all pages at once: reorder, rotate, delete and extract")
        self.a_organize.setCheckable(True)
        tb.addAction(self.a_organize)
        tb.widgetForAction(self.a_organize).setStyleSheet("font-weight: 600;")
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
        self.a_wifi = self._act("Share via Wi-Fi", self.wifi_clicked, None, SP.SP_DriveNetIcon,
                                "Use these tools from your phone on the same Wi-Fi")
        self.a_wifi.setCheckable(True)
        tb.addAction(self.a_wifi)
        self.theme_btn = QToolButton()
        self.theme_btn.setText("Theme")
        self.theme_btn.setToolTip("Light or dark look")
        self.theme_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.theme_btn)
        group = QActionGroup(self)
        current = self.settings.get("theme", "system")
        for key, label in (("system", "Match system"), ("light", "Light"), ("dark", "Dark")):
            a = menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(key == current)
            a.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            group.addAction(a)
        self.theme_btn.setMenu(menu)
        tb.addWidget(self.theme_btn)
        self.a_folder = self._act("", self._show_current, None, SP.SP_DirOpenIcon,
                                  "Show file in Finder" if sys.platform == "darwin" else "Show file in Explorer")
        tb.addAction(self.a_folder)
        self.a_about = self._act("", self.about, "F1", SP.SP_MessageBoxInformation, "About")
        tb.addAction(self.a_about)
        self.a_update = self._act("Check for updates", self.check_updates, None, SP.SP_BrowserReload,
                                  "See if a newer version of dl_pdf_tools is available")
        tb.addAction(self.a_update)

    def _set_enabled(self, on):
        for a in (self.a_split, self.a_extract, self.a_compress, self.a_folder, self.a_organize):
            a.setEnabled(on)
        viewing = on and not self.organizing()
        for a in (self.a_prev, self.a_next, self.a_zin, self.a_zout, self.a_fitw, self.a_fitp):
            a.setEnabled(viewing)
        self.page_box.setEnabled(viewing)
        self.zoom_box.setEnabled(viewing)
        self.empty.setVisible(not on)

    def _title(self):
        name = f"{APP_NAME} (v{APP_VERSION})"
        self.setWindowTitle(f"{self.path.name} — {name}" if self.path else name)

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
        if not self.confirm_discard():
            doc.close()
            return
        password = None
        while doc.needs_pass and doc.is_encrypted:   # needs_pass stays True after unlocking; is_encrypted clears
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
        self.organize.set_doc(doc)
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
        self.organize.set_doc(None)
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
    def _run(self, title, fn, *args, on_done, **kwargs):
        """Run fn in a worker process behind a busy dialog, then call on_done(result)."""
        dlg = QProgressDialog(title, None, 0, 0, self)
        dlg.setWindowTitle(APP_NAME)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setMinimumWidth(360)
        dlg.show()
        self.busy = True

        def finished(result):
            self.busy = False
            dlg.close()
            on_done(result)

        def failed(msg):
            self.busy = False
            dlg.close()
            QMessageBox.critical(self, APP_NAME, f"{title.rstrip('…')} failed:\n\n{msg}")

        bridge().submit(fn, *args, on_done=finished, on_error=failed, **kwargs)

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

    # ---------------------------------------------------------------- organize
    def organizing(self):
        return self.stack.currentWidget() is self.organize

    def toggle_organize(self):
        on = self.a_organize.isChecked() and self.doc is not None
        self.a_organize.setChecked(on)
        if on:
            self._dock_was_visible = self.dock.isVisible()
            self.dock.hide()
            self.stack.setCurrentWidget(self.organize)
            self.organize.grid.setFocus()
        else:
            self.stack.setCurrentWidget(self.view)
            self.dock.setVisible(getattr(self, "_dock_was_visible", True))
        self._set_enabled(self.doc is not None)
        self._organize_changed()

    def _organize_changed(self):
        dirty = self.organize.is_dirty()
        self.a_organize.setText("Organize •" if dirty else "Organize")
        self.a_organize.setToolTip("Organize has unsaved changes" if dirty else
                                   "See all pages at once: reorder, rotate, delete and extract")

    def arrangement(self):
        """The pages the tools work on: Organize's order/rotation when it's open, else every page in order."""
        if self.organizing():
            return self.organize.entries()
        return list(range(self.doc.page_count))

    def save_organized(self):
        if not self.doc:
            return
        start = str(self.path.parent / f"{self.path.stem}_organized.pdf")
        p, _ = QFileDialog.getSaveFileName(self, "Save organized PDF", start, "PDF files (*.pdf)")
        if not p:
            return
        out = Path(p).with_suffix(".pdf")
        if out.resolve() == self.path.resolve():
            QMessageBox.warning(self, "Organize", "Choose a different file name; the original PDF is kept unchanged.")
            return
        if out.exists():
            out.unlink()
        ents = self.organize.entries()

        def done(path):
            self.organize.mark_saved()
            self._organize_changed()
            self._report("Organize", f"Saved {len(ents)} page{'s' if len(ents) != 1 else ''} "
                                     f"({human_size(Path(path).stat().st_size)}) to\n{path}", path, open_pdf=path)

        self._run("Saving…", engine.save_arranged, str(self.path), ents, str(out), self.password, on_done=done)

    def confirm_discard(self):
        """Before closing the document: offer to save Organize changes. False means stay."""
        if not self.organize.is_dirty():
            return True
        r = QMessageBox.question(self, "Organize", "Organize has unsaved changes. Save them as a new PDF first?",
                                 QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
                                 QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Save:
            self.save_organized()
            return False   # saving runs in the background; the user can open/close again afterwards
        return r == QMessageBox.StandardButton.Discard

    # ---------------------------------------------------------------- updates
    def check_updates(self, info=None):
        d = UpdateDialog(self, self.settings, info if isinstance(info, dict) else None)
        d.exec()
        save_settings(self.settings)

    def _auto_update_check(self):
        """Quietly check once a day; if there's a newer version, show it in the status bar."""
        if not self.settings.get("auto_update", True) or not updater.app_location():
            return
        if time.time() - float(self.settings.get("last_update_check", 0)) < 24 * 3600:
            return
        self._auto_worker = _Worker(lambda _r: updater.latest(), self)

        def done(info):
            self.settings["last_update_check"] = time.time()
            if updater.is_newer(info["version"]):
                self._update_info = info
                self.update_label.setText(f"⬆ Update available: {info['version']}")
                self.update_label.show()
        self._auto_worker.done.connect(done)
        self._auto_worker.start()

    # ---------------------------------------------------------------- theme
    def set_theme(self, key):
        self.settings["theme"] = key
        apply_theme(key)
        save_settings(self.settings)

    # ---------------------------------------------------------------- tools
    def split(self, ranges=""):
        if not self.doc:
            return
        ents = self.arrangement()
        d = SplitDialog(self, self.path, len(ents), ranges if isinstance(ranges, str) else "")
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        out = d.out.path()
        groups = [[ents[i] for i in g] for g in d.result_groups]
        self._run("Splitting…", engine.split_groups, str(self.path), groups, str(out), self.password,
                  on_done=lambda files: self._report(
                      "Split PDF", f"Created {len(files)} file{'s' if len(files) != 1 else ''} in\n{out}", out))

    def extract(self, positions=None):
        if not self.doc:
            return
        ents = self.arrangement()
        if not isinstance(positions, list) or not positions:
            if self.organizing():
                positions = self.organize.selected_positions() or [0]
            else:
                positions = self.thumbs.selected_pages() if len(self.thumbs.selectedItems()) > 1 \
                    else [self.view.current_page()]
        saved = {"compress": self.settings.get("compress"), "extract_each": self.settings.get("extract_each", False)}
        d = ExtractDialog(self, self.path, ents, positions, self.password, saved)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        self.settings["extract_each"] = d.saved.get("extract_each", False)
        if d.result_compress:
            self.settings["compress"] = d.saved.get("compress")
        sel, out, comp = d.result_pages, d.result_path, d.result_compress
        n = len(sel)
        if d.result_one:
            self._run("Extracting pages…", engine.extract, str(self.path), sel, str(out), self.password, comp,
                      on_done=lambda p: self._report(
                          "Extract pages", f"Saved {n} page{'s' if n != 1 else ''} ({human_size(Path(p).stat().st_size)})"
                                           f" to\n{p}", p, open_pdf=p))
        else:
            self._run("Extracting pages…", engine.extract_each, str(self.path), sel, str(out), self.password,
                      compress=comp,
                      on_done=lambda files: self._report("Extract pages", f"Created {len(files)} files in\n{out}", out))

    def compress(self):
        if not self.doc:
            return
        arranged = self.organizing() and self.organize.is_dirty()
        ents = self.organize.entries() if arranged else None
        d = CompressDialog(self, self.path, self.password, self.settings, ents)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        self.settings["compress"] = d.opts.remember()
        src = self.path
        before = src.stat().st_size

        def done(r):
            if arranged:   # extract() returns the path of the organized, reduced copy
                r = {"before": before, "after": Path(r).stat().st_size, "written": True, "path": r}
            if not r["written"]:
                QMessageBox.information(self, "Reduce file size",
                                        f"{src.name} is already well optimised ({human_size(r['before'])}).\n\n"
                                        "These settings couldn't make it smaller, so no new file was written. "
                                        "Try the Strong preset or a lower custom resolution.")
                return
            saved = 100 * (1 - r["after"] / r["before"])
            self._report("Reduce file size",
                         f"{human_size(r['before'])}  →  {human_size(r['after'])}   ({saved:.0f}% smaller)"
                         + ("\n(with your Organize changes)" if arranged else "") + f"\n\nSaved as {r['path']}",
                         r["path"], open_pdf=r["path"])

        if arranged:
            self._run("Compressing…", engine.extract, str(src), ents, str(d.result_path), self.password,
                      d.opts.options(), on_done=done)
        else:
            self._run("Compressing…", engine.compress, str(src), str(d.result_path), password=self.password,
                      on_done=done, **d.opts.options())

    def _show_current(self):
        if self.path:
            show_in_folder(self.path)

    # ---------------------------------------------------------------- Share via Wi-Fi
    def _build_wifi_note(self):
        """Reminder bar: PDFs people opened over Wi-Fi are still stored on this computer."""
        bar = self.wifi_note = QWidget()
        bar.setObjectName("wifiNote")
        h = QHBoxLayout(bar)
        h.setContentsMargins(12, 6, 12, 6)
        self.wifi_note_lbl = QLabel()
        self.wifi_note_lbl.setStyleSheet(f"color: {WARN_COLOR}; font-weight: 600;")
        h.addWidget(self.wifi_note_lbl, 1)
        for text, fn in (("Open folder", self.wifi_open_folder), ("Delete all…", lambda: self.wifi_delete_all()),
                         ("Admin…", self.wifi_admin), ("Hide", self._hide_wifi_note)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            h.addWidget(b)
        bar.hide()
        self.wifi_note_timer = QTimer(self)
        self.wifi_note_timer.timeout.connect(self.refresh_wifi_note)
        self.wifi_note_timer.start(10000)
        QTimer.singleShot(1500, self.refresh_wifi_note)
        return bar

    def refresh_wifi_note(self):
        """Show the reminder while Wi-Fi uploads are stored (after Hide: only once more arrive)."""
        try:
            n, size = wifi_store.stats()
        except Exception:
            n, size = 0, 0
        if self.wifi_dlg is not None:
            self.wifi_dlg.stored(n, size)
        if n and (self._wifi_hidden is None or n > self._wifi_hidden):
            self._wifi_hidden = None
            self.wifi_note_lbl.setText(f"⚠  {n} Wi-Fi upload{'s' * (n != 1)} ({human_size(size)}) stored on this "
                                       "computer. Delete them when you no longer need them.")
            self.wifi_note.show()
        else:
            self.wifi_note.hide()

    def _hide_wifi_note(self):
        self._wifi_hidden = wifi_store.stats()[0]
        self.wifi_note.hide()

    def wifi_open_folder(self):
        wifi_store.WORK.mkdir(parents=True, exist_ok=True)
        show_in_folder(wifi_store.WORK)

    def wifi_delete_all(self, parent=None):
        """Ask, then delete every stored upload and its results. Returns True when deleted."""
        parent = parent or self
        n, size = wifi_store.stats()
        if not n:
            QMessageBox.information(parent, "Wi-Fi uploads", "There are no stored uploads.")
            return False
        if QMessageBox.question(parent, "Delete all Wi-Fi uploads?", (
                f"Delete {n} upload{'s' * (n != 1)} ({human_size(size)}) that people opened over Wi-Fi, with "
                "their results?\n\nThey're deleted for good (not to the Recycle Bin / Trash). Anyone still viewing "
                "them can no longer use them. The activity log is kept.")) != QMessageBox.StandardButton.Yes:
            return False
        self.wifi_command("purge")      # the sharing server lets go of the open PDFs first
        wifi_store.delete_all()
        self.refresh_wifi_note()
        return True

    def wifi_admin(self):
        if self.wifi_admin_dlg is None:
            self.wifi_admin_dlg = WifiAdminDialog(self)
        self.wifi_admin_dlg.show()
        self.wifi_admin_dlg.raise_()
        self.wifi_admin_dlg.activateWindow()

    def wifi_command(self, do, **params):
        """Tell the running sharing server something (quit, purge, kick). True if it answered."""
        st = self.wifi_state
        if self.wifi_proc is None or self.wifi_proc.poll() is not None or not st:
            return False
        q = urllib.parse.urlencode({"do": do, **params})
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{st['port']}/api/admin?{q}",
                                         headers={"X-Quit": st["quit"]})
            urllib.request.urlopen(req, timeout=3).read()
            return True
        except Exception:
            return False

    def wifi_clicked(self):
        self.a_wifi.setChecked(self.wifi_proc is not None)   # the button shows the state; the dialog changes it
        if self.wifi_proc is None:
            self.wifi_start()
        self.wifi_dialog().show()
        self.wifi_dialog().raise_()

    def wifi_dialog(self):
        if self.wifi_dlg is None:
            self.wifi_dlg = WifiDialog(self)
            self.wifi_dlg.turnOff.connect(self.wifi_stop)
            self.wifi_dlg.turnOn.connect(self.wifi_start)
            self.refresh_wifi_note()
        return self.wifi_dlg

    def wifi_start(self):
        from .web import WIFI_STATE
        WIFI_STATE.unlink(missing_ok=True)
        self.wifi_proc = subprocess.Popen(_self_command("--wifi", str(os.getpid())),
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.wifi_urls = None
        self.wifi_waited = 0
        self.a_wifi.setChecked(True)
        self.wifi_dialog().starting()
        self.wifi_timer.start(250)

    def _wifi_poll(self):
        from .web import WIFI_STATE
        self.wifi_waited += 1
        try:
            st = json.loads(WIFI_STATE.read_text(encoding="utf-8"))
        except Exception:
            st = None
        failed = None
        if st and st.get("error"):
            failed = st["error"]
        elif st and st.get("urls"):
            self.wifi_state = st
            if st["urls"] != self.wifi_urls or self.wifi_waited % 6 == 0:  # the .local link follows the IP one
                self.wifi_urls = st["urls"]
                self.wifi_dialog().ready(st)
                self.wifi_label.setText(f"📶 Sharing on Wi-Fi: {st['urls'][0]}")
                self.wifi_label.show()
            else:
                self.wifi_dialog().counts(st)
            if st.get("mdns") != "pending" or self.wifi_waited > 160:
                self.wifi_timer.setInterval(1500)       # from now on just the upload count
            if self.wifi_proc.poll() is not None:
                failed = "The sharing server stopped unexpectedly."
            else:
                return
        elif self.wifi_proc.poll() is not None:
            failed = "The sharing server stopped unexpectedly."
        elif self.wifi_waited > 80:   # 20 seconds
            failed = "The sharing server didn't start in time."
        if failed:
            self.wifi_timer.stop()
            self._wifi_kill()
            self.wifi_dialog().off(failed)

    def wifi_stop(self):
        """Turn off. Open pages say "Wi-Fi sharing has ended" for a few seconds, then the server quits."""
        self.wifi_timer.stop()
        if not self.wifi_command("quit"):
            self._wifi_kill()
        self.wifi_proc, self.wifi_state = None, None      # it ends by itself after telling the pages
        self.a_wifi.setChecked(False)
        self.wifi_label.hide()
        if self.wifi_dlg is not None:
            self.wifi_dlg.off()

    def _wifi_kill(self):
        if self.wifi_proc is not None and self.wifi_proc.poll() is None:
            self.wifi_proc.kill()
        self.wifi_proc, self.wifi_state = None, None
        self.a_wifi.setChecked(False)
        self.wifi_label.hide()

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
        if self.busy:
            QMessageBox.information(self, APP_NAME, "Please wait for the current job to finish.")
            e.ignore()
            return
        if not self.confirm_discard():
            e.ignore()
            return
        if self.wifi_proc is not None:
            self.wifi_stop()
        jobs.shutdown()
        self.settings["geometry"] = bytes(self.saveGeometry()).hex()
        self.settings["pages_panel"] = self.dock.isVisible() if not self.organizing() else             getattr(self, "_dock_was_visible", True)
        self.settings["fit"] = self.view.fit or "zoom"
        self.settings["zoom"] = self.view.canvas.zoom
        save_settings(self.settings)
        self.close_doc()
        super().closeEvent(e)


def apply_theme(key):
    """'light', 'dark' or 'system'. Qt 6.8+ switches the whole app's colours live."""
    scheme = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(key, Qt.ColorScheme.Unknown)
    try:
        QApplication.styleHints().setColorScheme(scheme)
    except AttributeError:
        pass


class App(QApplication):
    """Handles macOS "Open With" / dropping a PDF on the Dock icon."""
    def __init__(self, argv):
        super().__init__(argv)
        self.window = None
        self.pending = None

    def event(self, e):
        if e.type() == e.Type.FileOpen:
            f = e.file()
            if self.window is not None:
                self.window.open_file(f)
            else:
                self.pending = f
            return True
        return super().event(e)


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"dl.{APP_NAME}")
        except Exception:
            pass
    app = App(argv)
    app.setApplicationName(APP_NAME)
    apply_theme(load_settings().get("theme", "system"))
    ico = asset("icon.ico")
    if ico:
        app.setWindowIcon(QIcon(str(ico)))
    win = MainWindow()
    app.window = win
    win.show()
    if app.pending:
        QTimer.singleShot(0, lambda: win.open_file(app.pending))
    QTimer.singleShot(300, jobs.warm_up)   # start the worker processes while the user picks a file
    files = [a for a in argv[1:] if not a.startswith("-")]
    if files:
        QTimer.singleShot(0, lambda: win.open_file(files[0]))
    return app.exec()
