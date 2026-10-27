import json
import pytest

from releasegate.config import load_config, ConfigError
from releasegate.paypal import SandboxPayPal, MockPayPal, PayPalError, make_paypal, request_id


def sandbox(fake, tmp_path):
    cfg = load_config({"PAYPAL_CLIENT_ID": "id-not-real", "PAYPAL_CLIENT_SECRET": "secret-not-real",
                       "PAYPAL_BASE_URL": fake.url, "RELEASEGATE_DATA": str(tmp_path)})
    return SandboxPayPal(cfg), cfg


def test_full_sandbox_call_sequence_and_shapes(fake_paypal, tmp_path):
    pp, _ = sandbox(fake_paypal, tmp_path)
    o = pp.create_order("job1", "25.00", "USD", "desc", "http://r", "http://c")
    assert o == {"order_id": "ORD123", "status": "PAYER_ACTION_REQUIRED", "approval_url": "https://sandbox.example/approve?token=ORD123"}
    a = pp.authorize_order("job1", "ORD123")
    assert a["authorization_id"] == "AUTH123"
    assert pp.capture_authorization("job1", "AUTH123")["capture_id"] == "CAP123"
    assert pp.void_authorization("job1", "AUTH123")["status"] == "VOIDED"

    reqs = fake_paypal.requests
    tok = reqs[0]
    assert tok["path"] == "/v1/oauth2/token" and tok["headers"]["authorization"].startswith("Basic ")
    assert "grant_type=client_credentials" in tok["body"]
    create = reqs[1]
    body = json.loads(create["body"])
    assert create["path"] == "/v2/checkout/orders" and body["intent"] == "AUTHORIZE"
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "25.00"}
    assert create["headers"]["authorization"] == "Bearer fake-token"
    assert create["headers"]["paypal-request-id"] == request_id("job1", "create")
    paths = [r["path"] for r in reqs]
    assert paths.count("/v1/oauth2/token") == 1  # token cached
    assert "/v2/checkout/orders/ORD123/authorize" in paths
    cap = next(r for r in reqs if r["path"].endswith("/capture"))
    assert json.loads(cap["body"]) == {"final_capture": True}
    assert any(r["path"] == "/v2/payments/authorizations/AUTH123/void" for r in reqs)


def test_errors_are_wrapped_without_secrets(fake_paypal, tmp_path):
    pp, _ = sandbox(fake_paypal, tmp_path)
    with pytest.raises(PayPalError) as ei:
        pp.authorize_order("job1", "NOPE")
    assert ei.value.status == 404 and ei.value.debug_id == "dbg1"
    assert "secret-not-real" not in str(ei.value) and "fake-token" not in str(ei.value)


def test_request_id_is_deterministic_per_action():
    assert request_id("a", "capture") == request_id("a", "capture") != request_id("a", "void")


def test_live_paypal_host_is_refused():
    with pytest.raises(ConfigError):
        load_config({"PAYPAL_BASE_URL": "https://api-m.paypal.com"})
    with pytest.raises(ConfigError):
        load_config({"ANTHROPIC_BASE_URL": "https://evil.example"})


def test_mode_selection_and_status_never_leaks_secrets(tmp_path):
    assert load_config({}).paypal_mode == "mock"
    assert load_config({"PAYPAL_CLIENT_ID": "x"}).paypal_mode == "mock"
    c = load_config({"PAYPAL_CLIENT_ID": "id-not-real", "PAYPAL_CLIENT_SECRET": "secret-not-real", "ANTHROPIC_API_KEY": "key-not-real"})
    assert c.paypal_mode == "sandbox" and c.ai_mode == "ai"
    dump = json.dumps(c.public_status())
    for s in ("id-not-real", "secret-not-real", "key-not-real"):
        assert s not in dump
    assert load_config({"PAYPAL_CLIENT_ID": "a", "PAYPAL_CLIENT_SECRET": "b", "RELEASEGATE_FORCE_MOCK": "1"}).paypal_mode == "mock"
    assert isinstance(make_paypal(load_config({"RELEASEGATE_DATA": str(tmp_path)})), MockPayPal)


def test_mock_rules_and_persistence(tmp_path):
    m = MockPayPal(tmp_path / "s.json")
    o = m.create_order("j", "1.00", "USD", "d", "http://x/return?job=j", "http://x/c")
    assert "token=" + o["order_id"] in o["approval_url"]
    a = m.authorize_order("j", o["order_id"])
    m2 = MockPayPal(tmp_path / "s.json")  # state survives a new process
    m2.void_authorization("j", a["authorization_id"])
    with pytest.raises(PayPalError):
        m2.capture_authorization("j", a["authorization_id"])  # cannot capture a voided auth


def test_test_card_flow_single_call_authorizes(fake_paypal, tmp_path):
    pp, _ = sandbox(fake_paypal, tmp_path)
    r = pp.authorize_with_test_card("job2", "10.00", "USD", "desc")
    assert r == {"order_id": "ORDC", "status": "COMPLETED", "authorization_id": "AUTH123", "authorization_status": "CREATED"}
    create = next(x for x in fake_paypal.requests if x["path"] == "/v2/checkout/orders")
    body = json.loads(create["body"])
    assert body["intent"] == "AUTHORIZE" and "card" in body["payment_source"] and "paypal" not in body["payment_source"]
    assert create["headers"]["paypal-request-id"] == request_id("job2", "create-card")


def test_wallet_order_has_no_invalid_payment_method_preference(fake_paypal, tmp_path):
    pp, _ = sandbox(fake_paypal, tmp_path)
    pp.create_order("job1", "25.00", "USD", "d", "http://r", "http://c")
    ctx = json.loads(next(r for r in fake_paypal.requests if r["path"] == "/v2/checkout/orders")["body"])["payment_source"]["paypal"]["experience_context"]
    assert "payment_method_preference" not in ctx  # IMMEDIATE_PAYMENT_ALLOWED is rejected by PayPal (400 INVALID_PARAMETER_VALUE)


def test_engine_test_card_path_mock(cfg):
    from releasegate.engine import Engine
    e = Engine(cfg)
    j = e.create_job("t", "Deliver an 8 second video.", "10.00")
    j = e.pay_with_test_card(j["id"])
    assert j["state"] == "AUTHORIZED" and j["paypal"]["authorization_id"].startswith("MOCK-AUTH")
