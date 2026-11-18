"""Share via Wi-Fi: uploads kept per device, activity log, blocking, Delete all and Turn off.

Runs the real sharing server in this process, with every file it keeps redirected to a temporary folder.
Two "devices" are simulated by connecting over 127.0.0.1 and over this computer's network address.
"""
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dlpt import web, wifi_store  # noqa: E402
from test_engine import make_sample  # noqa: E402


def call(base, path, body=None, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def j(res):
    return json.loads(res[1] or b"{}")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="dlpt_wifi_test_"))
    wifi_store.WORK = tmp / "wifi"
    wifi_store.LOG_CSV = tmp / "wifi_activity.csv"
    wifi_store.BLOCK_JSON = tmp / "wifi_blocked.json"
    web.WIFI_STATE = tmp / "wifi.json"
    web.WIFI_ENDING = tmp / "wifi_ending"
    ip = web.lan_ip()
    if not ip:
        print("no network on this machine: Wi-Fi test skipped")
        return
    sample = tmp / "sample.pdf"
    make_sample(sample, 6)
    pdf = sample.read_bytes()

    t = threading.Thread(target=web._serve_wifi, args=(None, (0,)), daemon=True)
    t.start()
    for _ in range(100):
        if web.WIFI_STATE.exists():
            break
        time.sleep(0.1)
    st = json.loads(web.WIFI_STATE.read_text())
    assert not st.get("error"), st
    port = st["port"]
    A = f"http://127.0.0.1:{port}"      # device A (this computer)
    B = f"http://{ip}:{port}"           # device B (the network address)
    admin = {"X-Quit": st["quit"]}

    # Other websites can't reach it (Host header check).
    assert call(A, "/api/info", headers={"Host": "evil.example"})[0] == 403
    info = j(call(A, "/api/info"))
    assert info["lan"] is True, info

    # A opens a PDF; it's stored in a readable folder and logged.
    up = j(call(A, "/api/upload?name=report.pdf", raw=pdf))
    assert len(up["pages"]) == 6, up
    folders = wifi_store.folders()
    assert len(folders) == 1 and "report.pdf" in folders[0].name, folders
    res = j(call(A, "/api/extract", {"id": up["id"], "pages": "1-2"}))
    assert call(A, res["url"])[0] == 200

    # B can't see or use A's PDF or download A's result.
    assert call(B, f"/api/page?id={up['id']}&n=0&w=100")[0] == 400
    assert call(B, "/api/extract", {"id": up["id"], "pages": "1"})[0] == 400
    assert call(B, res["url"])[0] == 404
    upb = j(call(B, "/api/upload?name=b.pdf", raw=pdf))
    time.sleep(0.3)
    st = json.loads(web.WIFI_STATE.read_text())
    assert (st["files"], st["devices"]) == (2, 2), st

    # Closing the PDF on the page keeps its files on this computer.
    call(A, "/api/forget", {"ids": [up["id"]]})
    assert len(wifi_store.folders()) == 2

    # Block B: its PDFs are dropped and it's turned away; A still works.
    wifi_store.block(ip, "test device")
    assert call(A, f"/api/admin?do=kick&ip={ip}", headers=admin)[0] == 200
    r = call(B, "/api/heartbeat")
    assert r[0] == 403 and j(r).get("blocked"), r
    r = call(B, "/")
    assert r[0] == 403 and b"Access ended" in r[1]
    assert call(A, "/api/heartbeat")[0] == 200
    wifi_store.unblock(ip)
    assert call(B, "/api/heartbeat")[0] == 200
    assert call(B, f"/api/page?id={upb['id']}&n=0&w=100")[0] == 400   # kicked: no longer open

    # Admin needs the secret.
    assert call(A, "/api/admin?do=purge")[0] == 403

    # Delete all: the server lets go of the open PDFs, then every folder can be deleted.
    up2 = j(call(A, "/api/upload?name=again.pdf", raw=pdf))
    assert call(A, "/api/admin?do=purge", headers=admin)[0] == 200
    wifi_store.delete_all()
    assert wifi_store.stats() == (0, 0), wifi_store.folders()
    assert call(A, f"/api/page?id={up2['id']}&n=0&w=100")[0] == 400

    # The activity log has the visits.
    time.sleep(2.5)
    actions = {r[6] for r in wifi_store.read_log()}
    for a in ("Opened PDF", "Extracted pages", "Downloaded", "Blocked by you", "Unblocked by you"):
        assert a in actions, (a, actions)

    # Turn off: pages hear "sharing has ended", then the server stops.
    assert call(A, "/api/admin?do=quit", headers=admin)[0] == 200
    time.sleep(0.5)
    r = call(A, "/api/heartbeat")
    assert r[0] == 503 and j(r).get("ended"), r
    t.join(web.LINGER + 10)
    assert not t.is_alive(), "the server didn't stop after Turn off"
    assert not web.WIFI_STATE.exists() and not web.WIFI_ENDING.exists()
    print("wifi ok")


if __name__ == "__main__":
    main()
