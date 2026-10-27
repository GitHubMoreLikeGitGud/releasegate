"""PayPal Orders v2 + Payments v2 layer (sandbox only) and a clearly labelled local mock.

Flow:  create order (intent=AUTHORIZE) -> buyer approves -> authorize order
       -> [delivery passes checks] capture authorization   (money moves)
       -> [delivery fails checks]  void authorization      (nothing is charged)

Credentials come only from PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET (see config.py).
Nothing in this module prints or logs a secret or an access token.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlparse

from .config import Config

# PayPal's public sandbox test card (fake; only works against the sandbox).
SANDBOX_TEST_CARD = {
    "name": "Sandbox Buyer", "number": "4111111111111111", "expiry": "2030-12", "security_code": "123",
    "billing_address": {"address_line_1": "1 Main St", "admin_area_2": "San Jose", "admin_area_1": "CA",
                        "postal_code": "95131", "country_code": "US"},
}

_NS = uuid.UUID("6f1c3a52-2b9e-4c53-8d0c-5a3f0a5c9e11")


class PayPalError(Exception):
    def __init__(self, message: str, status: int | None = None, debug_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.debug_id = debug_id


def request_id(job_id: str, action: str) -> str:
    """Deterministic PayPal-Request-Id so a retried capture/void/authorize is idempotent."""
    return str(uuid.uuid5(_NS, f"{job_id}:{action}"))


class SandboxPayPal:
    """Real calls to the PayPal *sandbox*. Needs PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET."""

    mode = "sandbox"

    def __init__(self, cfg: Config, timeout: float = 30.0):
        if not (cfg.paypal_client_id and cfg.paypal_client_secret):
            raise PayPalError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set for sandbox mode")
        self._id = cfg.paypal_client_id
        self._secret = cfg.paypal_client_secret
        self.base = cfg.paypal_base_url
        self.timeout = timeout
        self._token: str | None = None
        self._token_exp = 0.0

    # -- plumbing ---------------------------------------------------------
    def _http(self, method: str, path: str, *, body: dict | None = None, headers: dict | None = None,
              raw: bytes | None = None) -> dict:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Accept": "application/json"}
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                text = r.read().decode() or "{}"
                return json.loads(text)
        except urllib.error.HTTPError as ex:
            text = ex.read().decode(errors="replace")
            try:
                j = json.loads(text)
            except ValueError:
                j = {}
            msg = j.get("message") or j.get("error_description") or j.get("name") or "PayPal request failed"
            details = "; ".join((d.get("issue", "") + (" @" + d["field"] if d.get("field") else ""))
                                for d in j.get("details", []) if isinstance(d, dict))
            raise PayPalError(f"{method} {path.split('?')[0]} -> HTTP {ex.code}: {msg} {details}".strip(),
                              status=ex.code, debug_id=j.get("debug_id")) from None
        except urllib.error.URLError as ex:
            raise PayPalError(f"cannot reach PayPal sandbox: {ex.reason}") from None

    def _auth(self) -> dict:
        if not self._token or time.time() > self._token_exp - 60:
            basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
            j = self._http("POST", "/v1/oauth2/token", raw=urlencode({"grant_type": "client_credentials"}).encode(),
                           headers={"Authorization": f"Basic {basic}",
                                    "Content-Type": "application/x-www-form-urlencoded"})
            self._token = j["access_token"]
            self._token_exp = time.time() + int(j.get("expires_in", 300))
        return {"Authorization": f"Bearer {self._token}"}

    # -- Orders v2 --------------------------------------------------------
    def create_order(self, job_id: str, amount: str, currency: str, description: str,
                     return_url: str, cancel_url: str) -> dict:
        """Create an order with intent AUTHORIZE. Returns {order_id, status, approval_url}."""
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "reference_id": job_id,
                "custom_id": job_id,
                "description": description[:127],
                "amount": {"currency_code": currency, "value": amount},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "return_url": return_url,
                "cancel_url": cancel_url,
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
            }}},
        }
        j = self._http("POST", "/v2/checkout/orders", body=body,
                       headers={**self._auth(), "PayPal-Request-Id": request_id(job_id, "create")})
        links = {l.get("rel"): l.get("href") for l in j.get("links", [])}
        return {"order_id": j["id"], "status": j.get("status"),
                "approval_url": links.get("payer-action") or links.get("approve")}

    def authorize_with_test_card(self, job_id: str, amount: str, currency: str, description: str) -> dict:
        """Headless buyer approval (sandbox only): one Orders v2 call with a sandbox test card.

        With intent AUTHORIZE and a card payment_source PayPal places the authorization hold during order
        creation, so no browser approval is needed. The card is PayPal's public sandbox test card, not a
        real card, and this method refuses any host except the sandbox (or localhost for tests).
        Returns {order_id, status, authorization_id, authorization_status}.
        """
        host = urlparse(self.base).hostname
        if host not in ("api-m.sandbox.paypal.com", "localhost", "127.0.0.1"):
            raise PayPalError("test-card flow is sandbox only")
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "reference_id": job_id, "custom_id": job_id, "description": description[:127],
                "amount": {"currency_code": currency, "value": amount},
            }],
            "payment_source": {"card": dict(SANDBOX_TEST_CARD)},
        }
        j = self._http("POST", "/v2/checkout/orders", body=body,
                       headers={**self._auth(), "PayPal-Request-Id": request_id(job_id, "create-card")})
        try:
            auth = j["purchase_units"][0]["payments"]["authorizations"][0]
        except (KeyError, IndexError):
            raise PayPalError(f"card order {j.get('id')} was created ({j.get('status')}) but holds no authorization") from None
        return {"order_id": j["id"], "status": j.get("status"),
                "authorization_id": auth["id"], "authorization_status": auth.get("status")}

    def get_authorization(self, authorization_id: str) -> dict:
        j = self._http("GET", f"/v2/payments/authorizations/{authorization_id}", headers=self._auth())
        return {"authorization_id": j["id"], "status": j.get("status")}

    def get_capture(self, capture_id: str) -> dict:
        j = self._http("GET", f"/v2/payments/captures/{capture_id}", headers=self._auth())
        return {"capture_id": j["id"], "status": j.get("status")}

    def get_order(self, order_id: str) -> dict:
        j = self._http("GET", f"/v2/checkout/orders/{order_id}", headers=self._auth())
        return {"order_id": j["id"], "status": j.get("status")}

    def authorize_order(self, job_id: str, order_id: str) -> dict:
        """Authorize an approved order. Funds are held, not moved. Returns {authorization_id, status}."""
        j = self._http("POST", f"/v2/checkout/orders/{order_id}/authorize", body={},
                       headers={**self._auth(), "PayPal-Request-Id": request_id(job_id, "authorize")})
        try:
            auth = j["purchase_units"][0]["payments"]["authorizations"][0]
        except (KeyError, IndexError):
            raise PayPalError("authorize response had no authorization (was the order approved by the buyer?)") from None
        return {"authorization_id": auth["id"], "status": auth.get("status", j.get("status"))}

    # -- Payments v2 ------------------------------------------------------
    def capture_authorization(self, job_id: str, authorization_id: str) -> dict:
        j = self._http("POST", f"/v2/payments/authorizations/{authorization_id}/capture",
                       body={"final_capture": True},
                       headers={**self._auth(), "PayPal-Request-Id": request_id(job_id, "capture")})
        return {"capture_id": j["id"], "status": j.get("status")}

    def void_authorization(self, job_id: str, authorization_id: str) -> dict:
        # PayPal answers 204 No Content on success.
        self._http("POST", f"/v2/payments/authorizations/{authorization_id}/void",
                   headers={**self._auth(), "PayPal-Request-Id": request_id(job_id, "void")})
        return {"status": "VOIDED"}


class MockPayPal:
    """LOCAL TESTING ONLY. Simulates the same calls with a small JSON state file. No network, no money.

    Used automatically when PAYPAL_CLIENT_ID/PAYPAL_CLIENT_SECRET are absent. The real demo must use
    SandboxPayPal. Every evidence-log entry records which mode produced it.
    """

    mode = "mock"

    def __init__(self, state_path=None):
        self._path = Path(state_path) if state_path else None
        self._s = {"orders": {}, "auths": {}}
        if self._path and self._path.exists():
            self._s = json.loads(self._path.read_text())

    def _save(self):
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._s))

    def create_order(self, job_id, amount, currency, description, return_url, cancel_url) -> dict:
        oid = "MOCK-ORDER-" + uuid.uuid4().hex[:12].upper()
        self._s["orders"][oid] = {"status": "PAYER_ACTION_REQUIRED", "job": job_id}
        self._save()
        sep = "&" if "?" in return_url else "?"
        return {"order_id": oid, "status": "PAYER_ACTION_REQUIRED",
                "approval_url": f"{return_url}{sep}token={oid}"}

    def authorize_with_test_card(self, job_id, amount, currency, description) -> dict:
        o = self.create_order(job_id, amount, currency, description, "http://mock/return", "http://mock/cancel")
        a = self.authorize_order(job_id, o["order_id"])
        return {"order_id": o["order_id"], "status": "COMPLETED", "authorization_id": a["authorization_id"],
                "authorization_status": a["status"]}

    def get_authorization(self, authorization_id) -> dict:
        a = self._s["auths"].get(authorization_id)
        if not a:
            raise PayPalError("mock: unknown authorization", status=404)
        return {"authorization_id": authorization_id, "status": a["status"]}

    def get_order(self, order_id) -> dict:
        o = self._s["orders"].get(order_id)
        if not o:
            raise PayPalError("mock: unknown order", status=404)
        return {"order_id": order_id, "status": o["status"]}

    def authorize_order(self, job_id, order_id) -> dict:
        o = self._s["orders"].get(order_id)
        if not o:
            raise PayPalError("mock: unknown order", status=404)
        if o.get("auth"):
            return {"authorization_id": o["auth"], "status": "CREATED"}
        aid = "MOCK-AUTH-" + uuid.uuid4().hex[:12].upper()
        o["status"] = "COMPLETED"
        o["auth"] = aid
        self._s["auths"][aid] = {"status": "CREATED"}
        self._save()
        return {"authorization_id": aid, "status": "CREATED"}

    def capture_authorization(self, job_id, authorization_id) -> dict:
        a = self._s["auths"].get(authorization_id)
        if not a:
            raise PayPalError("mock: unknown authorization", status=404)
        if a["status"] == "VOIDED":
            raise PayPalError("mock: authorization already voided", status=422)
        a["status"] = "CAPTURED"
        self._save()
        return {"capture_id": "MOCK-CAP-" + uuid.uuid4().hex[:12].upper(), "status": "COMPLETED"}

    def void_authorization(self, job_id, authorization_id) -> dict:
        a = self._s["auths"].get(authorization_id)
        if not a:
            raise PayPalError("mock: unknown authorization", status=404)
        if a["status"] == "CAPTURED":
            raise PayPalError("mock: authorization already captured", status=422)
        a["status"] = "VOIDED"
        self._save()
        return {"status": "VOIDED"}


def make_paypal(cfg: Config):
    return SandboxPayPal(cfg) if cfg.paypal_mode == "sandbox" else MockPayPal(cfg.data_dir / "mock_paypal.json")
