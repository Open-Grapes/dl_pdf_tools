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


def _subset_bytes(src_doc, pages, fast=False):
    new = pymupdf.open()
    try:
        for a, b in _runs(pages):
            new.insert_pdf(src_doc, from_page=a, to_page=b)
        return new.tobytes(garbage=1) if fast else new.tobytes(garbage=3, deflate=True)
    finally:
        new.close()


def _shrunk_bytes(pdf_bytes, opts):
    d = pymupdf.open("pdf", pdf_bytes)
    try:
        shrink_doc(d, **opts)
        return d.tobytes(**SAVE_SMALL)
    finally:
        d.close()


def _write_pages(src_doc, pages, out_path, compress=None):
    """Copy pages into a new PDF. With compress (a dict of compression options) the smaller of the
    plain and the compressed version is written."""
    data = _subset_bytes(src_doc, pages)
    if compress:
        small = _shrunk_bytes(data, compress)
        if len(small) < len(data):
            data = small
    out_path = unique_path(out_path)
    Path(out_path).write_bytes(data)
    return out_path


def _label(pages):
    a, b = pages[0] + 1, pages[-1] + 1
    contiguous = pages == list(range(pages[0], pages[-1] + 1))
    if a == b:
        return f"p{a}"
    return f"p{a}-{b}" if contiguous else f"p{a}-{b}_selection"


# ---------------------------------------------------------------- split / extract

def extract(src, pages, out_path, password=None, compress=None):
    """Copy the given 0-based pages (in that order) into one new PDF. Returns the path written.
    compress: optional dict of compression options (see compress()) to also shrink the result."""
    doc = open_doc(src, password)
    try:
        return _write_pages(doc, pages, out_path, compress)
    finally:
        doc.close()


def extract_each(src, pages, out_dir, password=None, progress=None, compress=None):
    """Write every given page to its own PDF. Returns the list of paths."""
    doc = open_doc(src, password)
    stem, out = Path(src).stem, []
    try:
        width = len(str(doc.page_count))
        for i, p in enumerate(pages):
            out.append(_write_pages(doc, [p], Path(out_dir) / f"{stem}_p{p + 1:0{width}d}.pdf", compress))
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

SAVE_SMALL = dict(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True, use_objstms=1)


def options_for(preset="balanced", dpi=150, quality=72, gray=False, strip_meta=False, images=True):
    """Compression options dict from a preset name ('light', 'balanced', 'strong' or 'custom')."""
    if preset in PRESETS:
        pr = PRESETS[preset]
        dpi, quality, threshold = pr["dpi"], pr["quality"], pr["threshold"]
    else:
        threshold = int(dpi * 1.3)
    return {"dpi": int(dpi), "threshold": int(threshold), "quality": int(quality), "gray": bool(gray),
            "strip_meta": bool(strip_meta), "images": bool(images)}


def shrink_doc(doc, dpi=150, threshold=None, quality=72, gray=False, strip_meta=False, images=True, step=None):
    """Compress an open document in place (call SAVE_SMALL save afterwards).

    Downsamples/recompresses big images, subsets embedded fonts, drops embedded page thumbnails and
    optionally metadata.
    """
    def say(msg):
        if step:
            step(msg)

    if images:
        say("Recompressing images…")
        doc.rewrite_images(dpi_threshold=threshold or int(dpi * 1.3), dpi_target=dpi, quality=quality,
                           lossy=True, lossless=True, bitonal=False, color=True, gray=True, set_to_gray=gray)
    say("Optimising fonts…")
    try:
        doc.subset_fonts()
    except Exception:
        pass  # some fonts cannot be subset; the rest of the compression still applies
    say("Removing extras…")
    try:
        doc.scrub(attached_files=False, clean_pages=False, embedded_files=False, hidden_text=False,
                  javascript=False, metadata=strip_meta, redactions=False, redact_images=0,
                  remove_links=False, reset_fields=False, reset_responses=False, thumbnails=True,
                  xml_metadata=strip_meta)
    except Exception:
        pass


SAMPLE_PAGES = 8


def estimate(src, opts, pages=None, password=None, sample=SAMPLE_PAGES):
    """Estimate the size compression would give, quickly.

    pages=None estimates compressing the whole file; a page list estimates extracting those pages with
    compression. Small jobs are compressed for real in memory (exact); bigger ones compress a sample of
    evenly spaced pages and scale the ratio up.
    Returns dict(base, size, exact): base is the size without compression.
    """
    doc = open_doc(src, password)
    try:
        target = list(pages) if pages else list(range(doc.page_count))
        whole = pages is None
        if whole and len(target) <= sample:
            base = os.path.getsize(src)
            d = open_doc(src, password)
            try:
                shrink_doc(d, **opts)
                size = len(d.tobytes(**SAVE_SMALL))
            finally:
                d.close()
            return {"base": base, "size": min(size, base), "exact": True}
        picked = target if len(target) <= sample else \
            [target[round(i * (len(target) - 1) / (sample - 1))] for i in range(sample)]
        picked = list(dict.fromkeys(picked))
        exact = len(picked) == len(target) and not whole
        plain = _subset_bytes(doc, picked, fast=not exact)
        small = len(_shrunk_bytes(plain, opts))
        if exact:   # extracting a few pages: this is exactly what extract() will produce
            return {"base": len(plain), "size": min(small, len(plain)), "exact": True}
        scale = len(target) / len(picked)
        base = os.path.getsize(src) if whole else int(len(plain) * scale)
        return {"base": base, "size": min(base, int(small * scale)), "exact": False}
    finally:
        doc.close()


def compress(src, dst, dpi=150, threshold=None, quality=72, gray=False, strip_meta=False,
             images=True, password=None, progress=None):
    """Write a smaller copy of src to dst.

    Runs shrink_doc, then saves with full garbage collection, stream compression and object streams.
    Returns dict(before, after, written, path). If the result is not smaller, nothing is written.
    """
    before = os.path.getsize(src)
    doc = open_doc(src, password)
    tmp_fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="dlpt_")
    os.close(tmp_fd)
    try:
        shrink_doc(doc, dpi=dpi, threshold=threshold, quality=quality, gray=gray, strip_meta=strip_meta,
                   images=images, step=progress)
        if progress:
            progress("Saving…")
        doc.save(tmp, **SAVE_SMALL)
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
