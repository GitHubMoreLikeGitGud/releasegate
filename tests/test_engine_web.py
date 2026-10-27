import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from releasegate.config import load_config
from releasegate.engine import Engine, JobError
from releasegate.paypal import SandboxPayPal
from releasegate.web import make_handler

BRIEF = "Deliver an 8 second video, at least 720p."


def flow(eng, sample, brief=BRIEF):
    j = eng.create_job("t", brief, "25.00")
    eng.start_payment(j["id"], "http://127.0.0.1:1")
    eng.authorize(j["id"])
    eng.submit_deliverable(j["id"], sample)
    return j["id"]


def events(eng, jid):
    return [e["event"] for e in eng.evidence(jid)["entries"]]


def test_pass_captures(cfg, samples):
    eng = Engine(cfg)
    jid = flow(eng, samples["good"])
    r = eng.run_checks(jid)
    assert r["result"]["decision"] == "CAPTURE" and r["job"]["state"] == "CAPTURED"
    ev = events(eng, jid)
    assert ev[-1] == "payment_captured" and "authorization_voided" not in ev
    assert eng.evidence(jid)["chain_ok"]


def test_fail_voids_and_never_captures(cfg, samples):
    eng = Engine(cfg)
    jid = flow(eng, samples["short"])
    r = eng.run_checks(jid)
    assert r["job"]["state"] == "VOIDED"
    ev = events(eng, jid)
    assert ev[-1] == "authorization_voided" and "payment_captured" not in ev
    with pytest.raises(JobError):
        eng.manual_settle(jid, "capture", "nope")  # voided is final


def test_preflight_settles_nothing(cfg, samples):
    eng = Engine(cfg)
    jid = flow(eng, samples["short"])
    n = len(eng.evidence(jid)["entries"])
    r = eng.run_checks(jid, settle=False)
    assert r["result"]["decision"] == "VOID" and not r["settled"]
    assert eng.get(jid)["state"] == "DELIVERED" and len(eng.evidence(jid)["entries"]) == n


def test_unverifiable_claim_holds_then_human_decides(cfg, samples, monkeypatch):
    from releasegate import checks as ck
    monkeypatch.setattr(ck, "ocr_backend", lambda: None)
    eng = Engine(cfg)
    jid = flow(eng, samples["titled"], BRIEF + ' The title card must say "Case Study".')
    r = eng.run_checks(jid)
    assert r["job"]["state"] == "HOLD" and r["result"]["decision"] == "HOLD"
    with pytest.raises(JobError):
        eng.manual_settle(jid, "capture", "")  # reason required
    eng.manual_settle(jid, "capture", "client confirmed the title card by eye")
    assert eng.get(jid)["state"] == "CAPTURED"
    assert "manual_override" in events(eng, jid)


def test_cannot_deliver_before_authorization_or_double_settle(cfg, samples):
    eng = Engine(cfg)
    j = eng.create_job("t", BRIEF, "5")
    with pytest.raises(JobError):
        eng.submit_deliverable(j["id"], samples["good"])
    jid = flow(eng, samples["good"])
    eng.run_checks(jid)
    with pytest.raises(JobError):
        eng.run_checks(jid)


def test_validation(cfg):
    eng = Engine(cfg)
    for amount in ("0", "-3", "abc", "100000"):
        with pytest.raises(JobError):
            eng.create_job("t", BRIEF, amount)
    with pytest.raises(JobError):
        eng.create_job("", BRIEF, "5")
    with pytest.raises(JobError):
        eng.get("../../etc/passwd")


def test_evidence_chain_detects_tampering(cfg, samples):
    eng = Engine(cfg)
    jid = flow(eng, samples["good"])
    eng.run_checks(jid)
    p = eng._dir(jid) / "evidence.jsonl"
    lines = p.read_text().splitlines()
    e = json.loads(lines[3])
    e["status"] = "SUPPORTED" if e.get("status") != "SUPPORTED" else "UNSUPPORTED"
    lines[3] = json.dumps(e)
    p.write_text("\n".join(lines) + "\n")
    ok, msg = eng.log(jid).verify_chain()
    assert not ok and "entry 4" in msg


def test_engine_with_sandbox_layer_against_fake_paypal(fake_paypal, samples, tmp_path):
    cfg = load_config({"PAYPAL_CLIENT_ID": "id-not-real", "PAYPAL_CLIENT_SECRET": "secret-not-real",
                       "PAYPAL_BASE_URL": fake_paypal.url, "RELEASEGATE_DATA": str(tmp_path / "d")})
    eng = Engine(cfg, SandboxPayPal(cfg))
    jid = flow(eng, samples["good"])
    assert eng.get(jid)["paypal_mode"] == "sandbox"
    eng.run_checks(jid)
    assert eng.get(jid)["paypal"]["capture_id"] == "CAP123"
    jid2 = flow(eng, samples["black"])
    eng.run_checks(jid2)
    assert eng.get(jid2)["state"] == "VOIDED"
    assert any(r["path"].endswith("/void") for r in fake_paypal.requests)
    log_text = open(eng._dir(jid) / "evidence.jsonl").read() + open(eng._dir(jid) / "job.json").read()
    assert "secret-not-real" not in log_text and "fake-token" not in log_text


@pytest.fixture
def web(cfg):
    eng = Engine(cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(eng, lambda: base))
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield base
    srv.shutdown()


def call(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_web_end_to_end(web, samples):
    assert call(web + "/api/status")[1]["paypal_mode"] == "mock"
    s, j = call(web + "/api/jobs", json.dumps({"title": "t", "brief": BRIEF, "amount": "10"}).encode())
    assert s == 200
    jid = j["id"]
    s, j = call(f"{web}/api/jobs/{jid}/pay", b"")
    assert j["state"] == "AWAITING_APPROVAL"
    # follow the (mock) buyer approval link exactly like a browser redirect would
    urllib.request.urlopen(j["paypal"]["approval_url"]).read()
    assert call(f"{web}/api/jobs/{jid}")[1]["state"] == "AUTHORIZED"
    data = samples["short"].read_bytes()
    s, j = call(f"{web}/api/jobs/{jid}/upload", data, {"X-Filename": "v.mp4"})
    assert s == 200 and j["state"] == "DELIVERED"
    s, r = call(f"{web}/api/jobs/{jid}/check", b"")
    assert r["job"]["state"] == "VOIDED"
    ev = call(f"{web}/api/jobs/{jid}/evidence")[1]
    assert ev["chain_ok"] and ev["entries"][-1]["event"] == "authorization_voided"
    assert urllib.request.urlopen(web + "/").read().startswith(b"<!doctype html>")


def test_web_rejects_bad_input(web):
    assert call(web + "/api/jobs/zzzz")[0] == 404
    assert call(web + "/api/jobs", json.dumps({"title": "t", "brief": "b", "amount": "-1"}).encode())[0] == 400
    assert call(web + "/api/jobs/abcdefabcdef")[0] == 400


def test_verify_reads_status_back(cfg, samples):
    eng = Engine(cfg)
    jid = flow(eng, samples["good"])
    assert eng.verify_with_paypal(jid)["authorization"] == "CREATED"
    eng.run_checks(jid)
    v = eng.verify_with_paypal(jid)
    assert v["job_state"] == "CAPTURED" and v["authorization"] == "CAPTURED"


def test_ui_script_is_valid_javascript():
    import re, shutil, subprocess, tempfile
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    html = (Path(__file__).parent.parent / "releasegate/static/index.html").read_text()
    js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
    r = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
