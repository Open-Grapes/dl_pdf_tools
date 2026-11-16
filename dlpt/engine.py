"""PDF operations: page ranges, split, extract and compress. No GUI code here, so it can be tested on its own."""

import os
import shutil
import tempfile
from pathlib import Path

import pymupdf

# Compression presets: images above `threshold` DPI are resampled down to `dpi` and re-saved as JPEG at `quality`.
PRESETS = {
    "light":    {"label": "Light – best quality",      "dpi": 220, "threshold": 300, "quality": 85},
    "balanced": {"label": "Balanced – recommended",    "dpi": 150, "threshold": 200, "quality": 72},
    "strong":   {"label": "Strong – smallest file",    "dpi": 96,  "threshold": 130, "quality": 55},
}


class RangeError(ValueError):
    pass


# ---------------------------------------------------------------- page ranges

def parse_groups(text, page_count):
    """Parse '1-3, 5, 8-' into groups of 0-based page numbers: [[0,1,2], [4], [7..end]].

    Accepted pieces: N, N-M, N- (to the end), -M (from the start), 'all'. Pages are 1-based for the user.
    """
    text = (text or "").replace(";", ",").replace("–", "-").replace("—", "-").strip()
    if not text:
        raise RangeError("Enter some pages, for example 1-3, 5, 8-")
    groups = []
    for raw in text.split(","):
        part = raw.strip().lower().replace(" ", "")
        if not part:
            continue
        if part in ("all", "*"):
            a, b = 1, page_count
        elif "-" in part:
            left, _, right = part.partition("-")
            a = _num(left, raw) if left else 1
            b = _num(right, raw) if right not in ("", "end") else page_count
        else:
            a = b = _num(part, raw)
        if a < 1 or b > page_count:
            raise RangeError(f"'{raw.strip()}' is outside the document (pages 1–{page_count}).")
        if a > b:
            raise RangeError(f"'{raw.strip()}' goes backwards; write the lower page first.")
        groups.append(list(range(a - 1, b)))
    if not groups:
        raise RangeError("Enter some pages, for example 1-3, 5, 8-")
    return groups


def _num(s, raw):
    try:
        return int(s)
    except ValueError:
        raise RangeError(f"'{raw.strip()}' is not a page number or range.") from None


def parse_pages(text, page_count):
    """Flat, de-duplicated, ordered list of 0-based pages from a range string."""
    seen, out = set(), []
    for g in parse_groups(text, page_count):
        for p in g:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def format_ranges(pages):
    """[0,1,2,4,7,8] -> '1-3, 5, 8-9' (1-based)."""
    pages = sorted(set(pages))
    out, i = [], 0
    while i < len(pages):
        j = i
        while j + 1 < len(pages) and pages[j + 1] == pages[j] + 1:
            j += 1
        a, b = pages[i] + 1, pages[j] + 1
        out.append(str(a) if a == b else f"{a}-{b}")
        i = j + 1
    return ", ".join(out)


# ---------------------------------------------------------------- helpers

def open_doc(path, password=None):
    doc = pymupdf.open(str(path))
    if doc.needs_pass and not doc.authenticate(password or ""):
        doc.close()
        raise PermissionError("This PDF is password protected and the password was not accepted.")
    if not doc.is_pdf:
        doc.close()
        raise ValueError("This file is not a PDF.")
    return doc


def unique_path(path):
    path = Path(path)
    if not path.exists():
        return path
    for i in range(2, 10000):
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not cand.exists():
            return cand
    raise FileExistsError(path)


def _runs(pages):
    """Split a page list into contiguous ascending runs so insert_pdf can copy them in one go."""
    runs = []
    for p in pages:
        if runs and p == runs[-1][1] + 1:
            runs[-1][1] = p
        else:
            runs.append([p, p])
    return runs


def _write_pages(src_doc, pages, out_path):
    new = pymupdf.open()
    try:
        for a, b in _runs(pages):
            new.insert_pdf(src_doc, from_page=a, to_page=b)
        out_path = unique_path(out_path)
        new.save(str(out_path), garbage=3, deflate=True)
        return out_path
    finally:
        new.close()


def _label(pages):
    a, b = pages[0] + 1, pages[-1] + 1
    contiguous = pages == list(range(pages[0], pages[-1] + 1))
    if a == b:
        return f"p{a}"
    return f"p{a}-{b}" if contiguous else f"p{a}-{b}_selection"


# ---------------------------------------------------------------- split / extract

def extract(src, pages, out_path, password=None):
    """Copy the given 0-based pages (in that order) into one new PDF. Returns the path written."""
    doc = open_doc(src, password)
    try:
        return _write_pages(doc, pages, out_path)
    finally:
        doc.close()


def extract_each(src, pages, out_dir, password=None, progress=None):
    """Write every given page to its own PDF. Returns the list of paths."""
    doc = open_doc(src, password)
    stem, out = Path(src).stem, []
    try:
        width = len(str(doc.page_count))
        for i, p in enumerate(pages):
            out.append(_write_pages(doc, [p], Path(out_dir) / f"{stem}_p{p + 1:0{width}d}.pdf"))
            if progress:
                progress(i + 1, len(pages))
        return out
    finally:
        doc.close()


def split_every(src, n, out_dir, password=None, progress=None):
    """Split into files of n pages each (the last one may be shorter)."""
    doc = open_doc(src, password)
    try:
        count = doc.page_count
        groups = [list(range(i, min(i + n, count))) for i in range(0, count, n)]
    finally:
        doc.close()
    return split_groups(src, groups, out_dir, password, progress)


def split_groups(src, groups, out_dir, password=None, progress=None):
    """Write each group of 0-based pages to its own file."""
    doc = open_doc(src, password)
    stem, out = Path(src).stem, []
    try:
        width = max(2, len(str(len(groups))))
        for i, g in enumerate(groups):
            name = f"{stem}_part{i + 1:0{width}d}_{_label(g)}.pdf"
            out.append(_write_pages(doc, g, Path(out_dir) / name))
            if progress:
                progress(i + 1, len(groups))
        return out
    finally:
        doc.close()


# ---------------------------------------------------------------- compress

def compress(src, dst, dpi=150, threshold=None, quality=72, gray=False, strip_meta=False,
             images=True, password=None, progress=None):
    """Write a smaller copy of src to dst.

    Steps: downsample/recompress big images, subset embedded fonts, drop embedded page thumbnails
    (and optionally metadata), then save with full garbage collection, stream compression and object streams.
    Returns dict(before, after, written, path). If the result is not smaller, nothing is written.
    """
    def step(msg):
        if progress:
            progress(msg)

    before = os.path.getsize(src)
    doc = open_doc(src, password)
    tmp_fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="dlpt_")
    os.close(tmp_fd)
    try:
        if images:
            step("Recompressing images…")
            doc.rewrite_images(dpi_threshold=threshold or int(dpi * 1.3), dpi_target=dpi, quality=quality,
                               lossy=True, lossless=True, bitonal=False, color=True, gray=True,
                               set_to_gray=gray)
        step("Optimising fonts…")
        try:
            doc.subset_fonts()
        except Exception:
            pass  # some fonts cannot be subset; the rest of the compression still applies
        step("Removing extras…")
        try:
            doc.scrub(attached_files=False, clean_pages=False, embedded_files=False, hidden_text=False,
                      javascript=False, metadata=strip_meta, redactions=False, redact_images=0,
                      remove_links=False, reset_fields=False, reset_responses=False, thumbnails=True,
                      xml_metadata=strip_meta)
        except Exception:
            pass
        step("Saving…")
        doc.save(tmp, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True,
                 use_objstms=1)
        doc.close()
        doc = None
        after = os.path.getsize(tmp)
        if after >= before:
            return {"before": before, "after": before, "written": False, "path": None}
        dst = Path(dst)
        if dst.resolve() == Path(src).resolve():
            raise ValueError("Choose a different file name; the original can't be overwritten while it is open.")
        shutil.move(tmp, dst)
        return {"before": before, "after": after, "written": True, "path": dst}
    finally:
        if doc is not None:
            doc.close()
        if os.path.exists(tmp):
            os.remove(tmp)
