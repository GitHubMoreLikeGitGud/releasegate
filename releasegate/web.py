"""Tiny local web UI (standard library only). Binds to 127.0.0.1; not meant to be exposed to a network."""
from __future__ import annotations

import json
import re
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .config import Config
from .engine import Engine, JobError

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 500 * 1024 * 1024


def make_handler(eng: Engine, base_url_fn):
    class H(BaseHTTPRequestHandler):
        server_version = "ReleaseGate"

        def log_message(self, *a):  # keep the console quiet and free of request data
            pass

        # ---- helpers
        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def _redirect(self, to):
            self.send_response(303)
            self.send_header("Location", to)
            self.end_headers()

        def _body_json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 100_000:
                raise JobError("request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def _guard(self, fn):
            try:
                fn()
            except JobError as e:
                self._json({"error": str(e)}, 400)
            except (ValueError, KeyError):
                self._json({"error": "bad request"}, 400)
            except Exception as e:  # never leak internals or secrets
                self._json({"error": f"server error: {type(e).__name__}"}, 500)

        # ---- routes
        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                b = (STATIC / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            elif u.path == "/api/status":
                self._json(eng.cfg.public_status())
            elif u.path == "/api/jobs":
                self._guard(lambda: self._json(eng.list_jobs()))
            elif m := re.fullmatch(r"/api/jobs/([a-f0-9]{12})", u.path):
                self._guard(lambda: self._json(eng.get(m[1])))
            elif m := re.fullmatch(r"/api/jobs/([a-f0-9]{12})/evidence", u.path):
                self._guard(lambda: self._json(eng.evidence(m[1])))
            elif u.path == "/paypal/return":  # buyer comes back from PayPal (or the mock) after approving
                def go():
                    job = eng.authorize((q.get("job") or [""])[0], (q.get("token") or [None])[0])
                    self._redirect(f"/#{job['id']}")
                try:
                    go()
                except JobError as e:
                    self._json({"error": str(e)}, 400)
            elif u.path == "/paypal/cancel":
                self._redirect("/#" + re.sub(r"[^a-f0-9]", "", (q.get("job") or [""])[0])[:12])
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == "/api/jobs":
                def go():
                    d = self._body_json()
                    self._json(eng.create_job(d.get("title"), d.get("brief"), d.get("amount"), d.get("currency", "USD")))
                return self._guard(go)
            m = re.fullmatch(r"/api/jobs/([a-f0-9]{12})/(pay|paytest|authorize|upload|check|preflight|settle)", u.path)
            if not m:
                return self._json({"error": "not found"}, 404)
            jid, act = m.groups()

            def go():
                if act == "pay":
                    self._json(eng.start_payment(jid, base_url_fn()))
                elif act == "paytest":
                    self._json(eng.pay_with_test_card(jid))
                elif act == "authorize":
                    self._json(eng.authorize(jid))
                elif act == "upload":
                    n = int(self.headers.get("Content-Length") or 0)
                    if not (0 < n <= MAX_UPLOAD):
                        raise JobError("upload must be 1 byte to 500 MB")
                    name = re.sub(r"[^\w.\- ]", "_", self.headers.get("X-Filename", "upload.mp4"))[:100]
                    with tempfile.TemporaryDirectory() as td:
                        p = Path(td) / name
                        left = n
                        with p.open("wb") as f:
                            while left:
                                chunk = self.rfile.read(min(1 << 20, left))
                                if not chunk:
                                    raise JobError("upload ended early")
                                f.write(chunk)
                                left -= len(chunk)
                        self._json(eng.submit_deliverable(jid, p))
                elif act in ("check", "preflight"):
                    self._json(eng.run_checks(jid, settle=(act == "check")))
                elif act == "settle":
                    d = self._body_json()
                    self._json(eng.manual_settle(jid, d.get("action"), d.get("reason")))
            self._guard(go)

    return H


def serve(cfg: Config, port: int):
    eng = Engine(cfg)
    base = f"http://127.0.0.1:{port}"
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(eng, lambda: base))
    st = cfg.public_status()
    print(f"ReleaseGate on {base}  | PayPal: {st['paypal_mode'].upper()}"
          f"{' (LOCAL MOCK, not PayPal)' if st['paypal_mode'] == 'mock' else ''} | checklist: {st['checklist_mode']}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return srv
