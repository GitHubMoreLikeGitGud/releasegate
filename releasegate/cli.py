"""Command-line interface: releasegate <command>."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config, ConfigError
from .engine import Engine, JobError
from .samples import make_sample

ICON = {"SUPPORTED": "PASS", "UNSUPPORTED": "FAIL", "NOT_VERIFIED": "????"}


def _print_findings(findings):
    for f in findings:
        print(f"  [{ICON.get(f['status'], f['status'])}] {f['claim']}")
        print(f"         measured={json.dumps(f['measured'])} expected={json.dumps(f['expected'])}")
        if f.get("note"):
            print(f"         note: {f['note']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="releasegate", description="Release a PayPal payment only when the delivery passes checks.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show mode (sandbox or mock) without revealing secrets")
    s = sub.add_parser("serve", help="start the local web UI")
    s.add_argument("--port", type=int)
    c = sub.add_parser("create", help="create a job")
    c.add_argument("--title", required=True)
    c.add_argument("--brief", required=True)
    c.add_argument("--amount", required=True)
    c.add_argument("--currency", default="USD")
    for name, h in (("pay", "create the PayPal order; prints the approval URL"),
                    ("authorize", "authorize the order after the buyer approved it"),
                    ("show", "show a job"), ("evidence", "print the evidence log")):
        p = sub.add_parser(name, help=h)
        p.add_argument("job")
    pt = sub.add_parser("paytest", help="sandbox only: authorize with PayPal's sandbox test card, no browser approval")
    pt.add_argument("job")
    vf = sub.add_parser("verify", help="read-only: ask PayPal for the order / authorization / capture status of a job")
    vf.add_argument("job")
    sub.add_parser("list", help="list jobs")
    d = sub.add_parser("deliver", help="submit the deliverable file")
    d.add_argument("job")
    d.add_argument("file")
    k = sub.add_parser("check", help="run the checks and capture or void (use --dry for a pre-flight)")
    k.add_argument("job")
    k.add_argument("--dry", action="store_true", help="pre-flight only: do not log or settle")
    m = sub.add_parser("settle", help="manual capture/void for a job on HOLD")
    m.add_argument("job")
    m.add_argument("action", choices=["capture", "void"])
    m.add_argument("--reason", required=True)
    sm = sub.add_parser("samples", help="generate sample videos (good, short, silent, black, ...) with ffmpeg")
    sm.add_argument("dir")
    dm = sub.add_parser("demo", help="end-to-end run on generated samples (pass and fail)")
    dm.add_argument("--base-url", default="http://127.0.0.1:8765")
    a = ap.parse_args(argv)

    try:
        cfg = load_config()
        if a.cmd == "status":
            print(json.dumps(cfg.public_status(), indent=2))
            return 0
        if a.cmd == "serve":
            from .web import serve
            serve(cfg, a.port or cfg.port)
            return 0
        if a.cmd == "samples":
            for kind in ("good", "titled", "short", "silent", "black", "half_black", "lowres"):
                print("wrote", make_sample(Path(a.dir) / f"{kind}.mp4", kind))
            return 0
        eng = Engine(cfg)
        banner = f"[PayPal mode: {eng.paypal.mode.upper()}{' - LOCAL MOCK, NOT PAYPAL' if eng.paypal.mode == 'mock' else ''}]"
        if a.cmd == "create":
            j = eng.create_job(a.title, a.brief, a.amount, a.currency)
            print(banner)
            print("job", j["id"], "| checklist source:", j["checklist_source"])
            for c_ in j["checklist"]:
                print("  -", c_["description"])
        elif a.cmd == "pay":
            j = eng.start_payment(a.job, f"http://127.0.0.1:{cfg.port}")
            print(banner)
            print("order", j["paypal"]["order_id"])
            print("buyer approval URL (open it and approve with a SANDBOX buyer account):\n ", j["paypal"]["approval_url"])
            if eng.paypal.mode == "mock":
                print("mock mode: run `releasegate authorize JOB` directly; there is no real approval step")
        elif a.cmd == "paytest":
            j = eng.pay_with_test_card(a.job)
            print(banner, "state:", j["state"], "order:", j["paypal"]["order_id"], "authorization:", j["paypal"]["authorization_id"])
        elif a.cmd == "authorize":
            j = eng.authorize(a.job)
            print(banner, "state:", j["state"], "authorization:", j["paypal"].get("authorization_id"))
        elif a.cmd == "deliver":
            j = eng.submit_deliverable(a.job, a.file)
            print("state:", j["state"], "sha256:", j["deliverable"]["sha256"])
        elif a.cmd == "check":
            r = eng.run_checks(a.job, settle=not a.dry)
            print(banner)
            _print_findings(r["result"]["findings"])
            print(f"decision: {r['result']['decision']} ({r['result']['reason']})")
            print("state:", r["job"]["state"], "(dry run, nothing settled)" if a.dry else "")
        elif a.cmd == "verify":
            v = eng.verify_with_paypal(a.job)
            print(banner)
            print("local job state :", v["job_state"])
            for k in ("order", "authorization", "capture"):
                if k in v:
                    print(f"PayPal {k:<13}: {v[k]}")
        elif a.cmd == "settle":
            j = eng.manual_settle(a.job, a.action, a.reason)
            print("state:", j["state"])
        elif a.cmd == "show":
            print(json.dumps(eng.get(a.job), indent=2))
        elif a.cmd == "list":
            for j in eng.list_jobs():
                print(j["id"], j["state"], j["amount"], j["currency"], j["title"])
        elif a.cmd == "evidence":
            ev = eng.evidence(a.job)
            for e in ev["entries"]:
                print(f"{e['seq']:>3} {e['ts']} {e['event']:<22} {e.get('status', ''):<13} {e.get('claim', '')}")
            print("hash chain:", "OK" if ev["chain_ok"] else "BROKEN - " + ev["chain_msg"])
        elif a.cmd == "demo":
            return _demo(eng, cfg, a.base_url, banner)
        return 0
    except (JobError, ConfigError) as e:
        print("error:", e, file=sys.stderr)
        return 1


def _demo(eng, cfg, base_url, banner) -> int:
    import tempfile
    print(banner)
    tmp = Path(tempfile.mkdtemp(prefix="rg-demo-"))
    from .checks import ocr_backend
    brief = "Deliver an 8 second video, at least 720p, with a voiceover."
    if ocr_backend():
        brief += ' The title card must say "Case Study".'
    else:
        print("(no OCR backend installed: the on-screen text claim is left out of this demo brief)")
    for kind in ("titled", "short"):
        f = make_sample(tmp / f"{kind}.mp4", kind)
        j = eng.create_job(f"Demo ({kind})", brief, "25.00")
        if eng.paypal.mode == "sandbox":
            eng.pay_with_test_card(j["id"])  # headless buyer approval with PayPal's sandbox test card
        else:
            eng.start_payment(j["id"], base_url)
            eng.authorize(j["id"])
        eng.submit_deliverable(j["id"], f)
        r = eng.run_checks(j["id"])
        print(f"\n== {kind}: {r['result']['decision']} -> {r['job']['state']}")
        _print_findings(r["result"]["findings"])
    return 0
