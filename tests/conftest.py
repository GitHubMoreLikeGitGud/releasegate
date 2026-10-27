import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from releasegate.config import load_config  # noqa: E402
from releasegate.samples import make_sample  # noqa: E402


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    d = tmp_path_factory.mktemp("samples")
    return {k: make_sample(d / f"{k}.mp4", k) for k in ("good", "short", "silent", "black", "half_black", "lowres", "titled")}


@pytest.fixture
def cfg(tmp_path):
    """Mock-mode config, with NO api keys, regardless of the developer's environment."""
    return load_config({"RELEASEGATE_DATA": str(tmp_path / "data")})


class FakeServer:
    """Records requests and replies from a routing function. Used for fake PayPal / fake Anthropic."""

    def __init__(self, route):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                outer.requests.append({"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": raw.decode()})
                code, obj = route(self.command, self.path, raw.decode())
                b = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            do_GET = do_POST = _do

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def paypal_route(method, path, body):
    if path == "/v1/oauth2/token":
        return 200, {"access_token": "fake-token", "expires_in": 3600}
    if path == "/v2/checkout/orders" and method == "POST" and "\"card\"" in body:
        return 201, {"id": "ORDC", "status": "COMPLETED", "purchase_units": [{"payments": {"authorizations": [{"id": "AUTH123", "status": "CREATED"}]}}]}
    if path == "/v2/checkout/orders" and method == "POST":
        return 201, {"id": "ORD123", "status": "PAYER_ACTION_REQUIRED",
                     "links": [{"rel": "payer-action", "href": "https://sandbox.example/approve?token=ORD123"}]}
    if path == "/v2/checkout/orders/ORD123/authorize":
        return 201, {"id": "ORD123", "status": "COMPLETED",
                     "purchase_units": [{"payments": {"authorizations": [{"id": "AUTH123", "status": "CREATED"}]}}]}
    if path == "/v2/payments/authorizations/AUTH123/capture":
        return 201, {"id": "CAP123", "status": "COMPLETED"}
    if path == "/v2/payments/authorizations/AUTH123/void":
        return 204, None
    return 404, {"name": "RESOURCE_NOT_FOUND", "message": "nope", "debug_id": "dbg1"}


@pytest.fixture
def fake_paypal():
    s = FakeServer(paypal_route)
    yield s
    s.close()
