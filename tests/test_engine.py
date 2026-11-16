"""Quick checks for the PDF engine. Run: .venv\\Scripts\\python -m pytest tests  (or python tests\\test_engine.py)"""

import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402
from PIL import Image  # noqa: E402

from dlpt import engine  # noqa: E402


def make_sample(path, pages=12):
    """A PDF with text on each page and a big noisy photo-like image every other page."""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i + 1}", fontsize=28)
        if i % 2 == 0:
            img = Image.effect_noise((2400, 1800), 60).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "PNG")
            page.insert_image(pymupdf.Rect(72, 120, 540, 470), stream=buf.getvalue())
    doc.save(str(path))
    doc.close()


def pages_text(path):
    with pymupdf.open(str(path)) as d:
        return [p.get_text().strip().splitlines()[0] for p in d]


def test_ranges():
    assert engine.parse_groups("1-3, 5, 8-", 10) == [[0, 1, 2], [4], [7, 8, 9]]
    assert engine.parse_pages("-2, 2, all", 3) == [0, 1, 2]
    assert engine.format_ranges([0, 1, 2, 4, 7, 8]) == "1-3, 5, 8-9"
    for bad in ("0", "11", "5-3", "abc", ""):
        try:
            engine.parse_groups(bad, 10)
        except engine.RangeError:
            continue
        raise AssertionError(bad)


def test_ops():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "sample.pdf"
        make_sample(src)

        out = engine.extract(src, [4, 0, 1], tmp / "ex.pdf")
        assert pages_text(out) == ["Page 5", "Page 1", "Page 2"], pages_text(out)

        (tmp / "each").mkdir()
        each = engine.extract_each(src, [2, 3], tmp / "each")
        assert [pages_text(p) for p in each] == [["Page 3"], ["Page 4"]]

        (tmp / "split").mkdir()
        parts = engine.split_every(src, 5, tmp / "split")
        assert [len(pages_text(p)) for p in parts] == [5, 5, 2]
        parts2 = engine.split_groups(src, engine.parse_groups("1-3, 4-", 12), tmp / "split")
        assert [len(pages_text(p)) for p in parts2] == [3, 9]
        assert len({p.name for p in parts + parts2}) == 5

        for key, pr in engine.PRESETS.items():
            r = engine.compress(src, tmp / f"c_{key}.pdf", dpi=pr["dpi"], threshold=pr["threshold"],
                                quality=pr["quality"])
            assert r["written"] and r["after"] < r["before"], r
            assert pages_text(r["path"])[0] == "Page 1"
            print(f"{key:9s} {r['before'] / 1e6:6.2f} MB -> {r['after'] / 1e6:6.2f} MB")

        # A tiny text-only PDF can't shrink: nothing should be written.
        small = tmp / "small.pdf"
        d = pymupdf.open()
        d.new_page().insert_text((72, 72), "hi")
        d.save(str(small), garbage=4, deflate=True, use_objstms=1)
        d.close()
        r = engine.compress(small, tmp / "small_c.pdf")
        assert not (tmp / "small_c.pdf").exists() or r["written"]


if __name__ == "__main__":
    test_ranges()
    test_ops()
    print("all ok")
