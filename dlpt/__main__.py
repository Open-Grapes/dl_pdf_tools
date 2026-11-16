"""Entry point.

  dl_pdf_tools.exe [file.pdf]               open the window
  dl_pdf_tools.exe --web [--no-browser]     local web version: opens the tools in your browser
  dl_pdf_tools.exe --wifi [PARENT_PID]      Share via Wi-Fi server for phones (started by the window)
  dl_pdf_tools.exe --selftest FILE.pdf DIR  run every operation (in worker processes) and write DIR\\selftest.txt
"""

import sys
from pathlib import Path


def selftest(src, out_dir):
    from . import engine, jobs
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = []
    try:
        import pymupdf
        with pymupdf.open(src) as d:
            n = d.page_count
        log.append(f"pages {n}")
        bal = engine.options_for("balanced")
        log.append(f"estimate {jobs.run(engine.estimate, src, bal)}")
        log.append(f"estimate extract {jobs.run(engine.estimate, src, bal, [0])}")
        log.append(f"extract {jobs.run(engine.extract, src, [0], out_dir / 'extract.pdf')}")
        log.append(f"extract+compress {jobs.run(engine.extract, src, [0], out_dir / 'extract_small.pdf', None, bal)}")
        log.append(f"split {len(jobs.run(engine.split_every, src, max(1, n // 2), out_dir))} files")
        r = jobs.run(engine.compress, src, out_dir / "compressed.pdf", **bal)
        log.append(f"compress {r['before']} -> {r['after']} written={r['written']}")
        import qrcode
        q = qrcode.QRCode(border=2)
        q.add_data("http://192.168.0.2")
        q.make(fit=True)
        from zeroconf import ServiceInfo, Zeroconf  # noqa: F401  (Share via Wi-Fi libraries are bundled)
        log.append(f"wifi libs ok, qr {len(q.get_matrix())}")
        log.append("OK")
    except Exception as e:
        import traceback
        log.append(traceback.format_exc())
        log.append(f"FAILED {e}")
    finally:
        jobs.shutdown()
    (out_dir / "selftest.txt").write_text("\n".join(log), encoding="utf-8")
    return 0 if log[-1] == "OK" else 1


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--selftest"] and len(args) >= 3:
        return selftest(args[1], args[2])
    if args[:1] == ["--wifi"]:   # started by the window's "Share via Wi-Fi" switch: --wifi [PARENT_PID]
        from . import web
        return web.serve_wifi(int(args[1]) if len(args) > 1 and args[1].isdigit() else None)
    if args[:1] == ["--web"]:
        from . import web
        return web.serve(open_browser="--no-browser" not in args)
    from . import gui
    return gui.main([sys.argv[0]] + args)


if __name__ == "__main__":
    sys.exit(main())
