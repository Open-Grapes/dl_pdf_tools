"""Browser versions of the app.

1. This computer only: `dl_pdf_tools (Web)` launcher (`--web`). A server on 127.0.0.1 with a random
   per-session token, so other web pages can't use it. Uploads are temporary and deleted when it stops
   (a minute after the last page closes).

2. Share via Wi-Fi: started from the window (`--wifi PID`, its own process). Phones and laptops on the same
   network open http://192.168.x.x (or http://dl-pdf-tools.local), open a PDF and split, extract or compress
   it here; they download the results. No password, but the Host header is checked (other websites can't reach
   it), each device only sees its own files, blocked devices are turned away, every visit is logged, and
   uploads are kept on this computer (one folder each) until you delete them in Admin.

PDFs never leave this computer's network: the page talks only to this computer.
"""

import json
import os
import secrets
import shutil
import socket
import socketserver
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
from .common import APP_NAME, APP_VERSION, CONFIG_DIR, BUNDLE_DIR, asset, human_size

PREFERRED_PORT = 8775
IDLE_EXIT = 60          # seconds without any page open before the local server stops
STATE = CONFIG_DIR / "web.json"
WORK = CONFIG_DIR / "web"
MAX_UPLOAD = 4 * 1024 ** 3

# Share via Wi-Fi: the same page, reachable from a phone on the same network.
WIFI_STATE = CONFIG_DIR / "wifi.json"     # written once the server is listening (or with an "error")
WIFI_ENDING = CONFIG_DIR / "wifi_ending"  # a server that was turned off and is still saying "sharing has ended"
WIFI_PORTS = (80, PREFERRED_PORT, 0)      # port 80 gives short addresses like http://192.168.0.20
MDNS_HOST = "dl-pdf-tools"                # announced as http://dl-pdf-tools.local
LINGER = 8          # seconds an address keeps saying "sharing has ended" after Turn off (pages check every 4 s)
BLOCKED_MSG = f"The owner of this computer has ended your access to {APP_NAME}."
BLOCKED_PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                f"<title>{APP_NAME}</title><body style='font:17px system-ui,sans-serif;display:grid;place-items:center;"
                "min-height:90vh;margin:0;padding:16px;text-align:center;background:#f3f4f6;color:#1d2330'>"
                "<div><div style='font-size:42px'>&#9940;</div><h2>Access ended</h2><p>" + BLOCKED_MSG + "</p></div>")
_SEEN = {}


def _page_file():
    for base in (Path(__file__).parent, BUNDLE_DIR / "dlpt"):
        if (base / "web_page.html").exists():
            return base / "web_page.html"
    raise FileNotFoundError("web_page.html")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second copy grab the same port, and phones then reach either one at
    # random. Claim the port exclusively there instead.
    allow_reuse_address = os.name != "nt"

    def __init__(self, port, token, work=WORK, wifi_hosts=None):
        """wifi_hosts: None for the local-only version; for Share via Wi-Fi, the host names the phone may
        use (this PC's address and the .local name). Wi-Fi mode needs no token, like VLC's Wi-Fi sharing."""
        super().__init__(("0.0.0.0" if wifi_hosts else "127.0.0.1", port), Handler)
        self.token, self.work, self.wifi_hosts = token, work, wifi_hosts
        self.quit_key = secrets.token_urlsafe(24)
        self.on_change = None     # Wi-Fi: called after an upload (the window shows the count)

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer.server_bind() looks up the machine's network name (socket.getfqdn), which can hang
        # for a long time on Macs whose name doesn't resolve. That name is never used here, so skip it.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
        self.last_seen = time.time()
        self.lock = threading.Lock()
        self.render_lock = threading.Lock()   # MuPDF is not thread-safe: one render at a time in this process
        self.items = {}      # id -> {"id", "name", "src", "password", "doc", "ip"}
        self.results = {}    # id -> {"path", "name", "ip"}
        self.uploads = []    # Wi-Fi: (ip) of every PDF opened since sharing started
        self.closed = False  # Wi-Fi sharing turned off: answer "ended", then hang up
        self.conns = set()   # open browser connections (kept alive between requests)
        self.conns_lock = threading.Lock()

    def handle_error(self, request, client_address):
        """A browser that hangs up mid-answer (closed tab, phone left the Wi-Fi) is normal: no traceback."""
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def hang_up(self):
        """Turn off: cut every open browser connection, so pages notice right away."""
        self.closed = True
        with self.conns_lock:
            conns = list(self.conns)
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def forget(self, match):
        """Close and drop the open PDFs for which match(item) is true (their files stay on Wi-Fi)."""
        with self.lock:
            gone = [self.items.pop(i) for i, it in list(self.items.items()) if match(it)]
            for rid in [r for r, res in self.results.items() if res.get("ip") in {it.get("ip") for it in gone}
                        and match(res)]:
                self.results.pop(rid, None)
        for it in gone:
            with self.render_lock:
                try:
                    it["doc"].close()
                except Exception:
                    pass
        return gone

    def counts(self):
        """(PDFs opened since sharing started, devices that sent them)."""
        with self.lock:
            return len(self.uploads), len(set(self.uploads))


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

    def setup(self):
        super().setup()
        with self.server.conns_lock:
            self.server.conns.add(self.connection)

    def finish(self):
        with self.server.conns_lock:
            self.server.conns.discard(self.connection)
        try:
            super().finish()
        except OSError:
            pass

    # ---------------------------------------------------------------- helpers
    def _send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        headers = dict(headers or {})
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", headers.pop("Cache-Control", "no-store"))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _query(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    @property
    def wifi(self):
        return bool(self.server.wifi_hosts)

    def _ip(self):
        return self.client_address[0]

    def _allowed(self):
        """Same-machine Host header (blocks DNS rebinding) and the session token."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if self.wifi:   # the Host check still blocks other websites (DNS rebinding)
            return host in self.server.wifi_hosts
        if host not in ("127.0.0.1", "localhost"):
            return False
        tok = self.headers.get("X-Token") or self._query().get("t", "")
        return secrets.compare_digest(tok, self.server.token)

    def _gate(self, path):
        """Wi-Fi: sharing turned off, or this device blocked. Say so (the page shows a notice) and stop."""
        if not self.wifi:
            return False
        from .wifi_store import is_blocked
        if self.server.closed:
            self.close_connection = True
            self._send(503, {"error": "Wi-Fi sharing was turned off on the computer.", "ended": True},
                       headers={"Connection": "close"})
            return True
        ip = self._ip()
        if is_blocked(ip):
            self.close_connection = True
            if time.time() - _SEEN.get("blocked " + ip, 0) > 600:
                self._log("Tried to connect (blocked)")
                _SEEN["blocked " + ip] = time.time()
            if path.startswith("/api/"):
                self._send(403, {"error": BLOCKED_MSG, "blocked": True}, headers={"Connection": "close"})
            else:
                self._send(403, BLOCKED_PAGE, "text/html; charset=utf-8", {"Connection": "close"})
            return True
        return False

    def _log(self, action, details=""):
        if self.wifi:
            from .wifi_store import log_event
            log_event(self._ip(), self.headers.get("User-Agent", ""), action, details)

    def _admin_ok(self):
        """The desktop app talking to its own sharing server (same PC + its secret)."""
        return self.client_address[0] == "127.0.0.1" and self.wifi and \
            secrets.compare_digest(self.headers.get("X-Quit", ""), self.server.quit_key)

    def _json(self):
        try:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            raise BadRequest("bad request") from None

    def _item(self, iid):
        with self.server.lock:
            it = self.server.items.get(iid or "")
        # On Wi-Fi a device only gets its own files.
        if not it or (self.wifi and it.get("ip") != self._ip()):
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
        if path == "/api/admin":   # before the gate: it works while "ended" too
            return self._admin()
        if not self._allowed():
            return self._send(403, ("Open the address shown in the Share via Wi-Fi window." if self.wifi else
                                    f"Open the page with the {APP_NAME} (Web) launcher."), "text/plain")
        if self._gate(path):
            return
        self.server.last_seen = time.time()
        try:
            if path in ("/", "/index.html"):
                if self.wifi and time.time() - _SEEN.get(self._ip(), 0) > 600:   # once per 10 min per device
                    self._log("Opened the page")
                _SEEN[self._ip()] = time.time()
                return self._send(200, _page_file().read_bytes(), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                              "script-src 'self' 'unsafe-inline'; img-src 'self' data:"})
            if path == "/api/info":
                return self._send(200, {"app": APP_NAME, "version": APP_VERSION, "presets": engine.PRESETS,
                                        "lan": self.wifi, "pc": socket.gethostname()})
            if path == "/api/heartbeat":
                return self._send(200, {"ok": True})
            if path == "/api/page":
                return self._render()
            if path.startswith("/api/download/"):
                with self.server.lock:
                    r = self.server.results.get(path.rsplit("/", 1)[-1])
                if not r or (self.wifi and r.get("ip") != self._ip()) or not Path(r["path"]).is_file():
                    return self._send(404, {"error": "That download has expired. Run the tool again."})
                self._log("Downloaded", r["name"])
                return self._send_file(Path(r["path"]), r["name"])
        except BadRequest as e:
            return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def _admin(self):
        """Commands from the desktop app: end sharing, close every PDF (before Delete all), or drop a blocked
        device's PDFs."""
        if not self._admin_ok():
            return self._send(403, {"error": "forbidden"})
        q = self._query()
        do = q.get("do", "")
        if do == "quit":   # answer first: ending sharing cuts every open connection, this one included
            self.close_connection = True
            self._send(200, {"ok": True}, headers={"Connection": "close"})
            threading.Thread(target=end_sharing, args=(self.server,), daemon=True).start()
            return
        if do == "purge":
            self.server.forget(lambda it: True)
        elif do == "kick":
            ip = q.get("ip", "")
            self.server.forget(lambda it: it.get("ip") == ip)
        else:
            return self._send(400, {"error": "unknown"})
        return self._send(200, {"ok": True})

    def _render(self):
        q = self._query()
        it = self._item(q.get("id"))
        try:
            n, w = int(q.get("n", 0)), max(40, min(3000, int(q.get("w", 800))))
        except ValueError:
            raise BadRequest("bad page") from None
        with self.server.render_lock:
            doc = it["doc"]
            if doc.is_closed or not 0 <= n < doc.page_count:
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
        if self._gate(path):
            return
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
        if self.wifi:
            from .wifi_store import new_folder
            d = new_folder(iid[:6], self._ip(), name)
        else:
            d = self.server.work / iid
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
        it = {"id": iid, "name": name, "src": str(src), "password": None, "doc": doc, "ip": self._ip()}
        with self.server.lock:
            self.server.items[iid] = it
            if self.wifi:
                self.server.uploads.append(self._ip())
        if self.wifi:
            self._log("Opened PDF", f"{name} · {human_size(length)}")
            if self.server.on_change:
                self.server.on_change()
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
            self.server.results[rid] = {"path": str(path), "name": name, "ip": it.get("ip")}
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
        self._log("Split", f"{it['name']} → {len(files)} files")
        return self._result(it, files, f"{Path(it['name']).stem}_split.zip")

    def _extract(self):
        req = self._json()
        it = self._item(req.get("id"))
        pages, _ = self._pages(it, req.get("pages", ""))
        comp = self._opts(req["compress"]) if req.get("compress") else None
        out = self._outdir(it)
        stem = Path(it["name"]).stem
        what = f"{it['name']} · pages {engine.format_ranges(pages)}" + (" · reduced size" if comp else "")
        if req.get("separate"):
            files = jobs.run(engine.extract_each, it["src"], pages, str(out), it["password"], compress=comp)
            self._log("Extracted pages (separate files)", what)
            return self._result(it, files, f"{stem}_pages.zip")
        label = engine.format_ranges(pages).replace(", ", "_").replace(" ", "")
        label = label if len(label) <= 40 else label[:40] + "_etc"
        f = jobs.run(engine.extract, it["src"], pages, str(out / f"{stem}_pages_{label}.pdf"), it["password"], comp)
        self._log("Extracted pages", what)
        return self._result(it, [f], "")

    def _compress(self):
        req = self._json()
        it = self._item(req.get("id"))
        out = self._outdir(it) / f"{Path(it['name']).stem}_compressed.pdf"
        r = jobs.run(engine.compress, it["src"], str(out), password=it["password"], **self._opts(req.get("opts")))
        res = {"before": r["before"], "after": r["after"], "written": r["written"]}
        self._log("Reduced size", f"{it['name']} · {human_size(r['before'])} → {human_size(r['after'])}"
                  if r["written"] else f"{it['name']} · couldn't be made smaller")
        if r["written"]:
            res.update(self._result(it, [r["path"]], ""))
        return res

    def _forget(self):
        ids = set(self._json().get("ids", []))
        ip = self._ip()
        gone = self.server.forget(lambda it: it.get("id") in ids and (not self.wifi or it.get("ip") == ip))
        if not self.wifi:   # on Wi-Fi the files stay in the uploads folder until you delete them (Admin)
            for it in gone:
                shutil.rmtree(Path(it["src"]).parent, ignore_errors=True)
        return {"ok": True}


# ---------------------------------------------------------------- Share via Wi-Fi

def lan_ip():
    """This PC's address on the local network (the adapter that has the default route), or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))   # picks the route; nothing is sent
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") or ip.startswith("0.") else ip
    except OSError:
        return None


def _announce(ip, port):
    """Make http://dl-pdf-tools.local work on phones (mDNS / Bonjour). Returns a closer, or None."""
    try:
        from zeroconf import ServiceInfo, Zeroconf
        zc = Zeroconf(interfaces=[ip])
        info = ServiceInfo("_http._tcp.local.", f"{APP_NAME} on {socket.gethostname()}._http._tcp.local.",
                           port=port, addresses=[socket.inet_aton(ip)], server=f"{MDNS_HOST}.local.",
                           properties={"path": "/"})
        zc.register_service(info, allow_name_change=True)

        def close():
            try:
                zc.unregister_service(info)
            finally:
                zc.close()
        return close
    except Exception:
        return None   # the IP address link still works


_ending_lock = threading.Lock()


def end_sharing(srv, linger=LINGER):
    """Turn off: open pages are told "sharing has ended" for a few seconds, then the server stops."""
    with _ending_lock:
        if srv.closed:
            return
        srv.hang_up()
    try:
        WIFI_ENDING.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    time.sleep(linger)
    srv.shutdown()


def serve_wifi(parent_pid=None):
    """Run the page for phones on this network until the desktop app (parent_pid) turns it off or quits.

    Problems are written to wifi.json ("error") and wifi.log, because the windowed app has no console.
    """
    import faulthandler
    import traceback
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    crash_log = open(CONFIG_DIR / "wifi.log", "w", encoding="utf-8")
    faulthandler.enable(crash_log)
    faulthandler.dump_traceback_later(30, file=crash_log)   # if starting up ever hangs, record where
    try:
        return _serve_wifi(parent_pid)
    except BaseException as e:
        crash_log.write(traceback.format_exc())
        crash_log.flush()
        try:
            WIFI_STATE.write_text(json.dumps({"error": f"{e.__class__.__name__}: {e}"}), encoding="utf-8")
        except OSError:
            pass
        return 1


def _wait_for_previous():
    """Turned straight back on: let the previous server (still saying "sharing has ended") free its port, so
    the address stays the same."""
    t = time.time()
    while WIFI_ENDING.exists() and time.time() - t < LINGER + 3:
        try:
            if time.time() - WIFI_ENDING.stat().st_mtime > LINGER + 10:   # left over from a crash
                break
        except OSError:
            break
        time.sleep(0.25)


def _serve_wifi(parent_pid, ports=WIFI_PORTS):
    from .wifi_store import WORK as UPLOADS
    WIFI_STATE.unlink(missing_ok=True)
    ip = lan_ip()
    if not ip:
        WIFI_STATE.write_text(json.dumps({"error": "This PC isn't connected to a network."}), encoding="utf-8")
        return 1
    UPLOADS.mkdir(parents=True, exist_ok=True)        # earlier uploads are kept until you delete them
    _wait_for_previous()
    hosts = {ip, f"{MDNS_HOST}.local", f"{socket.gethostname().lower()}.local", "127.0.0.1", "localhost"}
    srv = None
    for port in ports:
        try:
            srv = Server(port, "", UPLOADS, hosts)
            break
        except OSError:
            continue
    if srv is None:
        WIFI_STATE.write_text(json.dumps({"error": "Couldn't open a network port."}), encoding="utf-8")
        return 1
    port = srv.server_address[1]
    suffix = "" if port == 80 else f":{port}"
    state = {"urls": [f"http://{ip}{suffix}"], "port": port, "pid": os.getpid(), "quit": srv.quit_key,
             "mdns": "pending", "files": 0, "devices": 0}
    state_lock = threading.Lock()
    stopped = []

    def write_state():
        if stopped:
            return
        with state_lock:
            state["files"], state["devices"] = srv.counts()
            tmp = WIFI_STATE.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(state), encoding="utf-8")
                os.replace(tmp, WIFI_STATE)
            except OSError:
                pass

    srv.on_change = write_state
    write_state()   # the IP address works right away
    import faulthandler
    faulthandler.cancel_dump_traceback_later()   # started fine
    closers = []

    def announce():   # the .local name follows when mDNS is ready; it can be slow or blocked on some networks
        close = _announce(ip, port)
        if close:
            closers.append(close)
            state["urls"].append(f"http://{MDNS_HOST}.local{suffix}")
        state["mdns"] = "on" if close else "off"
        write_state()
    threading.Thread(target=announce, daemon=True).start()
    if parent_pid:   # the app closed (or crashed): end sharing the same way as Turn off
        jobs.on_exit_of(parent_pid, lambda: end_sharing(srv))
    jobs.warm_up()
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        stopped.append(True)
        srv.server_close()
        for close in closers:
            try:
                close()
            except Exception:
                pass
        jobs.shutdown()
        srv.forget(lambda it: True)
        WIFI_STATE.unlink(missing_ok=True)
        WIFI_ENDING.unlink(missing_ok=True)
    return 0


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
        srv.forget(lambda it: True)
        shutil.rmtree(WORK, ignore_errors=True)
        try:
            STATE.unlink()
        except OSError:
            pass
    return 0
