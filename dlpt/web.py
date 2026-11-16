"""Local web version: a small server on 127.0.0.1 that shows the PDF tools in the browser.

PDFs opened on the page are uploaded to this computer's own server (nothing leaves the PC), viewed,
split, extracted or compressed with the same engine as the window, and results are offered as downloads.

  dl_pdf_tools.exe --web     start (or reuse) the server and open the page

Only this computer can reach it (bound to 127.0.0.1), and every request needs a random per-session token,
so other web pages open in the browser can't use it. The server stops itself a minute after the last
page is closed.
"""

import json
import os
import secrets
import shutil
import socket
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pymupdf

from . import engine, jobs
from .common import APP_NAME, APP_VERSION, CONFIG_DIR, BUNDLE_DIR, asset

PREFERRED_PORT = 8775
IDLE_EXIT = 60          # seconds without any page open before the server stops
STATE = CONFIG_DIR / "web.json"
WORK = CONFIG_DIR / "web"
MAX_UPLOAD = 4 * 1024 ** 3


def _page_file():
    for base in (Path(__file__).parent, BUNDLE_DIR / "dlpt"):
        if (base / "web_page.html").exists():
            return base / "web_page.html"
    raise FileNotFoundError("web_page.html")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, token):
        super().__init__(("127.0.0.1", port), Handler)
        self.token = token
        self.last_seen = time.time()
        self.lock = threading.Lock()
        self.render_lock = threading.Lock()   # MuPDF is not thread-safe: one render at a time in this process
        self.items = {}      # id -> {"name", "src", "password", "doc"}
        self.results = {}    # id -> {"path", "name"}


def _safe_name(name):
    name = Path(name.replace("\\", "/")).name
    name = "".join(c for c in name if c not in '<>:"/\\|?*' and ord(c) >= 32).strip(" .")
    return name or "file.pdf"


class BadRequest(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ---------------------------------------------------------------- helpers
    def _send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", (headers or {}).pop("Cache-Control", "no-store"))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _query(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    def _allowed(self):
        """Same-machine Host header (blocks DNS rebinding) and the session token."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        tok = self.headers.get("X-Token") or self._query().get("t", "")
        return secrets.compare_digest(tok, self.server.token)

    def _json(self):
        try:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            raise BadRequest("bad request") from None

    def _item(self, iid):
        with self.server.lock:
            it = self.server.items.get(iid or "")
        if not it:
            raise BadRequest("That PDF is no longer open here. Open it again.")
        return it

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/ping":   # lets a second launch find this server
            return self._send(200, {"app": APP_NAME, "ok": True})
        if path in ("/icon.png", "/favicon.ico"):
            ico = asset("icon.png")
            return self._send(200, ico.read_bytes() if ico else b"", "image/png", {"Cache-Control": "max-age=86400"})
        if not self._allowed():
            return self._send(403, f"Open the page with the {APP_NAME} (Web) launcher.", "text/plain")
        self.server.last_seen = time.time()
        try:
            if path in ("/", "/index.html"):
                return self._send(200, _page_file().read_bytes(), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                              "script-src 'self' 'unsafe-inline'; img-src 'self' data:"})
            if path == "/api/info":
                return self._send(200, {"app": APP_NAME, "version": APP_VERSION, "presets": engine.PRESETS})
            if path == "/api/heartbeat":
                return self._send(200, {"ok": True})
            if path == "/api/page":
                return self._render()
            if path.startswith("/api/download/"):
                with self.server.lock:
                    r = self.server.results.get(path.rsplit("/", 1)[-1])
                if not r or not Path(r["path"]).is_file():
                    return self._send(404, {"error": "That download has expired. Run the tool again."})
                return self._send_file(Path(r["path"]), r["name"])
        except BadRequest as e:
            return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def _render(self):
        q = self._query()
        it = self._item(q.get("id"))
        try:
            n, w = int(q.get("n", 0)), max(40, min(3000, int(q.get("w", 800))))
        except ValueError:
            raise BadRequest("bad page") from None
        with self.server.render_lock:
            doc = it["doc"]
            if not 0 <= n < doc.page_count:
                raise BadRequest("bad page")
            page = doc[n]
            pix = page.get_pixmap(matrix=pymupdf.Matrix(w / page.rect.width, w / page.rect.width), alpha=False)
            data = pix.tobytes("jpg", jpg_quality=88)
        return self._send(200, data, "image/jpeg", {"Cache-Control": "private, max-age=3600"})

    def _send_file(self, p, name):
        size = p.stat().st_size
        quoted = urllib.parse.quote(name)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip" if name.endswith(".zip") else "application/pdf")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(p, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1024 * 1024)

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        self.server.last_seen = time.time()
        routes = {"/api/upload": self._upload, "/api/unlock": self._unlock, "/api/estimate": self._estimate,
                  "/api/split": self._split, "/api/extract": self._extract, "/api/compress": self._compress,
                  "/api/forget": self._forget}
        fn = routes.get(path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            return self._send(200, fn())
        except (BadRequest, engine.RangeError, PermissionError, ValueError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # shown on the page
            return self._send(500, {"error": f"{e.__class__.__name__}: {e}"})

    def _upload(self):
        name = _safe_name(self._query().get("name", "file.pdf"))
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD:
            raise BadRequest("The file is empty or too large.")
        iid = secrets.token_hex(8)
        d = WORK / iid
        d.mkdir(parents=True, exist_ok=True)
        src = d / name
        left = length
        with open(src, "wb") as fh:
            while left > 0:
                chunk = self.rfile.read(min(left, 1024 * 1024))
                if not chunk:
                    break
                fh.write(chunk)
                left -= len(chunk)
                self.server.last_seen = time.time()
        if left:
            shutil.rmtree(d, ignore_errors=True)
            raise BadRequest("Upload interrupted.")
        try:
            with self.server.render_lock:
                doc = pymupdf.open(str(src))
                if not doc.is_pdf:
                    doc.close()
                    raise BadRequest(f"{name} is not a PDF.")
        except BadRequest:
            shutil.rmtree(d, ignore_errors=True)
            raise
        except Exception as e:
            shutil.rmtree(d, ignore_errors=True)
            raise BadRequest(f"Couldn't open {name}: {e}") from None
        it = {"id": iid, "name": name, "src": str(src), "password": None, "doc": doc}
        with self.server.lock:
            self.server.items[iid] = it
        return self._info(it)

    def _info(self, it):
        with self.server.render_lock:
            doc = it["doc"]
            if doc.needs_pass and doc.is_encrypted:   # still locked (is_encrypted clears once unlocked)
                return {"id": it["id"], "name": it["name"], "locked": True}
            sizes = [[round(p.rect.width, 1), round(p.rect.height, 1)] for p in doc]
        return {"id": it["id"], "name": it["name"], "locked": False, "size": os.path.getsize(it["src"]),
                "pages": sizes}

    def _unlock(self):
        req = self._json()
        it = self._item(req.get("id"))
        with self.server.render_lock:
            ok = it["doc"].authenticate(str(req.get("password", "")))
        if not ok:
            raise BadRequest("Wrong password.")
        it["password"] = str(req.get("password", ""))
        return self._info(it)

    @staticmethod
    def _opts(o):
        o = o or {}
        try:
            return engine.options_for(o.get("preset", "balanced"), int(o.get("dpi", 150)), int(o.get("quality", 72)),
                                      bool(o.get("gray")), bool(o.get("strip_meta")), bool(o.get("images", True)))
        except (TypeError, ValueError):
            raise BadRequest("bad options") from None

    def _pages(self, it, text):
        with self.server.render_lock:
            count = it["doc"].page_count
        return engine.parse_pages(text, count), count

    def _estimate(self):
        req = self._json()
        it = self._item(req.get("id"))
        pages = self._pages(it, req["pages"])[0] if req.get("pages") else None
        return jobs.run(engine.estimate, it["src"], self._opts(req.get("opts")), pages, it["password"])

    def _result(self, it, files, name):
        """Register output files for download (several are zipped). Returns the JSON for the page."""
        rid = secrets.token_hex(8)
        files = [Path(f) for f in files]
        if len(files) == 1:
            path, name = files[0], files[0].name
        else:
            path = files[0].parent / f"{rid}.zip"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for f in files:
                    z.write(f, f.name)
        with self.server.lock:
            self.server.results[rid] = {"path": str(path), "name": name}
        return {"url": f"/api/download/{rid}", "name": name, "files": len(files), "size": path.stat().st_size}

    def _outdir(self, it):
        d = Path(it["src"]).parent / f"out-{secrets.token_hex(4)}"
        d.mkdir()
        return d

    def _split(self):
        req = self._json()
        it = self._item(req.get("id"))
        with self.server.render_lock:
            count = it["doc"].page_count
        mode = req.get("mode")
        if mode == "every":
            n = max(1, int(req.get("n") or 1))
            groups = [list(range(i, min(i + n, count))) for i in range(0, count, n)]
        elif mode == "each":
            groups = [[i] for i in range(count)]
        else:
            groups = engine.parse_groups(req.get("ranges", ""), count)
        out = self._outdir(it)
        files = jobs.run(engine.split_groups, it["src"], groups, str(out), it["password"])
        return self._result(it, files, f"{Path(it['name']).stem}_split.zip")

    def _extract(self):
        req = self._json()
        it = self._item(req.get("id"))
        pages, _ = self._pages(it, req.get("pages", ""))
        comp = self._opts(req["compress"]) if req.get("compress") else None
        out = self._outdir(it)
        stem = Path(it["name"]).stem
        if req.get("separate"):
            files = jobs.run(engine.extract_each, it["src"], pages, str(out), it["password"], compress=comp)
            return self._result(it, files, f"{stem}_pages.zip")
        label = engine.format_ranges(pages).replace(", ", "_").replace(" ", "")
        label = label if len(label) <= 40 else label[:40] + "_etc"
        f = jobs.run(engine.extract, it["src"], pages, str(out / f"{stem}_pages_{label}.pdf"), it["password"], comp)
        return self._result(it, [f], "")

    def _compress(self):
        req = self._json()
        it = self._item(req.get("id"))
        out = self._outdir(it) / f"{Path(it['name']).stem}_compressed.pdf"
        r = jobs.run(engine.compress, it["src"], str(out), password=it["password"], **self._opts(req.get("opts")))
        res = {"before": r["before"], "after": r["after"], "written": r["written"]}
        if r["written"]:
            res.update(self._result(it, [r["path"]], ""))
        return res

    def _forget(self):
        for iid in self._json().get("ids", []):
            with self.server.lock:
                it = self.server.items.pop(iid, None)
            if it:
                with self.server.render_lock:
                    it["doc"].close()
                shutil.rmtree(Path(it["src"]).parent, ignore_errors=True)
        return {"ok": True}


# ---------------------------------------------------------------- start / reuse

def _existing():
    """URL of an already-running server, or None."""
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
        with urllib.request.urlopen(f"http://127.0.0.1:{st['port']}/ping", timeout=2) as r:
            if json.loads(r.read()).get("app") == APP_NAME:
                return f"http://127.0.0.1:{st['port']}/?t={st['token']}"
    except Exception:
        pass
    return None


def _free_port():
    for port in (PREFERRED_PORT, 0):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    return 0


def serve(open_browser=True):
    url = _existing()
    if url:
        if open_browser:
            webbrowser.open(url)
        return 0
    shutil.rmtree(WORK, ignore_errors=True)   # leftovers from a previous run
    WORK.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    srv = Server(_free_port(), token)
    port = srv.server_address[1]
    STATE.write_text(json.dumps({"port": port, "token": token, "pid": os.getpid()}), encoding="utf-8")
    url = f"http://127.0.0.1:{port}/?t={token}"

    def watchdog():
        while True:
            time.sleep(5)
            if time.time() - srv.last_seen > IDLE_EXIT:
                srv.shutdown()
                return
    threading.Thread(target=watchdog, daemon=True).start()
    jobs.warm_up()
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        srv.server_close()
        jobs.shutdown()
        with srv.lock:
            for it in srv.items.values():
                try:
                    it["doc"].close()
                except Exception:
                    pass
        shutil.rmtree(WORK, ignore_errors=True)
        try:
            STATE.unlink()
        except OSError:
            pass
    return 0
