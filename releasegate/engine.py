"""Job lifecycle: create -> authorize payment -> deliver -> check -> capture / void (or hold).

States: CREATED -> AWAITING_APPROVAL -> AUTHORIZED -> DELIVERED -> CAPTURED | VOIDED | HOLD
Every transition writes to the job's evidence log.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import checks as ck
from .checklist import build_checklist
from .config import Config
from .evidence import EvidenceLog
from .paypal import PayPalError, make_paypal

MAX_AMOUNT = Decimal("1000.00")
CURRENCIES = {"USD", "EUR", "GBP", "SGD", "AUD", "CAD"}
JOB_ID = re.compile(r"^[a-f0-9]{12}$")


class JobError(Exception):
    pass


class Engine:
    def __init__(self, cfg: Config, paypal=None):
        self.cfg = cfg
        self.paypal = paypal or make_paypal(cfg)
        self._lock = threading.RLock()
        (cfg.data_dir / "jobs").mkdir(parents=True, exist_ok=True)

    # ---- storage ---------------------------------------------------------
    def _dir(self, job_id: str) -> Path:
        if not JOB_ID.match(job_id or ""):
            raise JobError("invalid job id")
        return self.cfg.data_dir / "jobs" / job_id

    def log(self, job_id: str) -> EvidenceLog:
        return EvidenceLog(self._dir(job_id) / "evidence.jsonl")

    def get(self, job_id: str) -> dict:
        p = self._dir(job_id) / "job.json"
        if not p.exists():
            raise JobError("job not found")
        return json.loads(p.read_text())

    def _save(self, job: dict):
        (self._dir(job["id"]) / "job.json").write_text(json.dumps(job, indent=2))

    def list_jobs(self) -> list[dict]:
        out = []
        for p in sorted((self.cfg.data_dir / "jobs").glob("*/job.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            j = json.loads(p.read_text())
            out.append({k: j[k] for k in ("id", "title", "amount", "currency", "state", "created")})
        return out

    def evidence(self, job_id: str) -> dict:
        lg = self.log(job_id)
        ok, msg = lg.verify_chain()
        return {"entries": lg.entries(), "chain_ok": ok, "chain_msg": msg}

    # ---- lifecycle -------------------------------------------------------
    def create_job(self, title: str, brief: str, amount: str, currency: str = "USD") -> dict:
        title, brief = (title or "").strip(), (brief or "").strip()
        if not title or not brief:
            raise JobError("title and brief are required")
        try:
            amt = Decimal(str(amount)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            raise JobError("amount must be a number") from None
        if not (Decimal("0.01") <= amt <= MAX_AMOUNT):
            raise JobError(f"amount must be between 0.01 and {MAX_AMOUNT} (sandbox demo limit)")
        currency = (currency or "USD").upper()
        if currency not in CURRENCIES:
            raise JobError("unsupported currency")
        job_id = uuid.uuid4().hex[:12]
        self._dir(job_id).mkdir(parents=True)
        checklist, source = build_checklist(self.cfg, brief)
        job = {"id": job_id, "title": title[:120], "brief": brief[:4000], "amount": str(amt), "currency": currency,
               "state": "CREATED", "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "checklist": checklist,
               "checklist_source": source, "paypal_mode": self.paypal.mode, "paypal": {}, "deliverable": None,
               "result": None}
        self._save(job)
        lg = self.log(job_id)
        lg.append("job_created", paypal_mode=self.paypal.mode, amount=job["amount"], currency=currency,
                  claim="The job brief was recorded as written", status="SUPPORTED")
        lg.append("checklist_built", source=source, checks=[{"id": c["id"], "type": c["type"], "params": c["params"],
                                                              "required": c["required"]} for c in checklist],
                  claim="The brief was turned into a verifiable checklist", status="SUPPORTED",
                  method="AI-assisted" if source.startswith("ai") else "rule-based")
        return job

    def start_payment(self, job_id: str, base_url: str) -> dict:
        """Create the PayPal order (intent AUTHORIZE). Returns the job; job['paypal']['approval_url'] is where the buyer approves."""
        with self._lock:
            job = self.get(job_id)
            if job["state"] not in ("CREATED", "AWAITING_APPROVAL"):
                raise JobError(f"payment already started (state {job['state']})")
            ret = f"{base_url}/paypal/return?job={job_id}"
            cancel = f"{base_url}/paypal/cancel?job={job_id}"
            try:
                o = self.paypal.create_order(job_id, job["amount"], job["currency"], f"ReleaseGate job: {job['title']}", ret, cancel)
            except PayPalError as e:
                self._fail(job, "order_create_failed", e)
            job["paypal"].update({"order_id": o["order_id"], "approval_url": o["approval_url"]})
            job["state"] = "AWAITING_APPROVAL"
            self._save(job)
            self.log(job_id).append("order_created", paypal_mode=self.paypal.mode, order_id=o["order_id"], intent="AUTHORIZE",
                                    claim="An AUTHORIZE order exists; no money has moved", status="SUPPORTED")
            return job

    def pay_with_test_card(self, job_id: str) -> dict:
        """Sandbox/mock only: create the AUTHORIZE order and place the hold in one call, no browser approval."""
        with self._lock:
            job = self.get(job_id)
            if job["state"] not in ("CREATED", "AWAITING_APPROVAL"):
                raise JobError(f"payment already started (state {job['state']})")
            try:
                a = self.paypal.authorize_with_test_card(job_id, job["amount"], job["currency"],
                                                         f"ReleaseGate job: {job['title']}")
            except PayPalError as e:
                self._fail(job, "order_create_failed", e)
            job["paypal"].update({"order_id": a["order_id"], "authorization_id": a["authorization_id"]})
            job["state"] = "AUTHORIZED"
            self._save(job)
            self.log(job_id).append("order_created", paypal_mode=self.paypal.mode, order_id=a["order_id"], intent="AUTHORIZE",
                                    buyer_approval="sandbox test card (no browser)",
                                    claim="An AUTHORIZE order exists; no money has moved", status="SUPPORTED")
            self.log(job_id).append("payment_authorized", paypal_mode=self.paypal.mode, authorization_id=a["authorization_id"],
                                    paypal_status=a["authorization_status"],
                                    claim="Funds are held (authorized) but not captured", status="SUPPORTED")
            return job

    def authorize(self, job_id: str, order_id: str | None = None) -> dict:
        """After the buyer approves, place the authorization hold."""
        with self._lock:
            job = self.get(job_id)
            if job["state"] == "AUTHORIZED" or job["state"] in ("DELIVERED", "CAPTURED", "VOIDED", "HOLD"):
                return job
            oid = job["paypal"].get("order_id")
            if not oid or (order_id and order_id != oid):
                raise JobError("order id does not match this job")
            try:
                a = self.paypal.authorize_order(job_id, oid)
            except PayPalError as e:
                self._fail(job, "authorize_failed", e)
            job["paypal"]["authorization_id"] = a["authorization_id"]
            job["state"] = "AUTHORIZED"
            self._save(job)
            self.log(job_id).append("payment_authorized", paypal_mode=self.paypal.mode, authorization_id=a["authorization_id"],
                                    paypal_status=a["status"], claim="Funds are held (authorized) but not captured",
                                    status="SUPPORTED")
            return job

    def submit_deliverable(self, job_id: str, src: str | Path) -> dict:
        with self._lock:
            job = self.get(job_id)
            if job["state"] not in ("AUTHORIZED", "DELIVERED"):
                raise JobError(f"cannot deliver in state {job['state']}: payment must be authorized first")
            src = Path(src)
            if not src.is_file():
                raise JobError("deliverable file not found")
            ext = re.sub(r"[^a-z0-9]", "", src.suffix.lower())[:6] or "bin"
            dest = self._dir(job_id) / f"deliverable.{ext}"
            for old in self._dir(job_id).glob("deliverable.*"):
                if old != dest:
                    old.unlink()
            if src.resolve() != dest.resolve():
                shutil.copyfile(src, dest)
            h = hashlib.sha256()
            with dest.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            job["deliverable"] = {"filename": re.sub(r"[^\w.\- ]", "_", src.name)[:100], "stored": dest.name,
                                  "sha256": h.hexdigest(), "bytes": dest.stat().st_size}
            job["state"] = "DELIVERED"
            self._save(job)
            self.log(job_id).append("deliverable_received", sha256=h.hexdigest(), bytes=dest.stat().st_size,
                                    filename=job["deliverable"]["filename"],
                                    claim="This exact file (by SHA-256) was the one checked", status="SUPPORTED")
            return job

    def run_checks(self, job_id: str, settle: bool = True) -> dict:
        """Run the checklist. settle=False is a pre-flight: findings are returned but nothing is logged or settled."""
        with self._lock:
            job = self.get(job_id)
            if job["state"] != "DELIVERED":
                raise JobError(f"nothing to check in state {job['state']}")
            path = self._dir(job_id) / job["deliverable"]["stored"]
            findings = ck.run_checklist(path, job["checklist"])
            decision, reason = ck.decide(findings)
            result = {"decision": decision, "reason": reason, "findings": [f.to_dict() for f in findings]}
            if not settle:
                return {"job": job, "result": result, "settled": False}
            lg = self.log(job_id)
            for f in findings:
                lg.append("check_result", check_id=f.check_id, check_type=f.type, claim=f.claim, status=f.status,
                          measured=f.measured, expected=f.expected, method=f.method, note=f.note, required=f.required)
            lg.append("decision", decision=decision, reason=reason, claim=f"Delivery meets the brief: {decision == 'CAPTURE'}",
                      status="SUPPORTED" if decision == "CAPTURE" else "UNSUPPORTED" if decision == "VOID" else "NOT_VERIFIED")
            job["result"] = {"decision": decision, "reason": reason}
            self._save(job)
            if decision == "CAPTURE":
                self._capture(job)
            elif decision == "VOID":
                self._void(job)
            else:
                job["state"] = "HOLD"
                self._save(job)
                lg.append("held_for_review", claim="Authorization left open for a human decision", status="NOT_VERIFIED",
                          paypal_mode=self.paypal.mode)
            return {"job": self.get(job_id), "result": result, "settled": True}

    def manual_settle(self, job_id: str, action: str, reason: str) -> dict:
        """Human override for a HOLD. Always logged with the reason."""
        with self._lock:
            job = self.get(job_id)
            if job["state"] != "HOLD":
                raise JobError("manual settlement is only for jobs on HOLD")
            if action not in ("capture", "void") or not (reason or "").strip():
                raise JobError("action must be capture or void, with a reason")
            self.log(job_id).append("manual_override", action=action, reason=reason.strip()[:300],
                                    claim="A person, not the checks, made this decision", status="SUPPORTED")
            (self._capture if action == "capture" else self._void)(job)
            return self.get(job_id)

    def verify_with_paypal(self, job_id: str) -> dict:
        """Ask PayPal (a read-only GET) what it says about this job's order, authorization and capture."""
        job = self.get(job_id)
        pp = job["paypal"]
        out = {"mode": self.paypal.mode, "job_state": job["state"]}
        try:
            if pp.get("order_id"):
                out["order"] = self.paypal.get_order(pp["order_id"])["status"]
            if pp.get("authorization_id"):
                out["authorization"] = self.paypal.get_authorization(pp["authorization_id"])["status"]
            if pp.get("capture_id") and hasattr(self.paypal, "get_capture"):
                out["capture"] = self.paypal.get_capture(pp["capture_id"])["status"]
        except PayPalError as e:
            raise JobError(f"PayPal error: {e}") from e
        return out

    # ---- settlement ------------------------------------------------------
    def _capture(self, job: dict):
        try:
            c = self.paypal.capture_authorization(job["id"], job["paypal"]["authorization_id"])
        except PayPalError as e:
            self._fail(job, "capture_failed", e)
        job["paypal"]["capture_id"] = c["capture_id"]
        job["state"] = "CAPTURED"
        self._save(job)
        self.log(job["id"]).append("payment_captured", paypal_mode=self.paypal.mode, capture_id=c["capture_id"],
                                   paypal_status=c["status"], claim="Payment was captured after checks passed", status="SUPPORTED")

    def _void(self, job: dict):
        try:
            self.paypal.void_authorization(job["id"], job["paypal"]["authorization_id"])
        except PayPalError as e:
            self._fail(job, "void_failed", e)
        job["state"] = "VOIDED"
        self._save(job)
        self.log(job["id"]).append("authorization_voided", paypal_mode=self.paypal.mode,
                                   claim="Authorization was voided; the client was not charged", status="SUPPORTED")

    def _fail(self, job: dict, event: str, err: PayPalError):
        self.log(job["id"]).append(event, error=str(err)[:300], debug_id=err.debug_id, paypal_mode=self.paypal.mode,
                                   claim="A PayPal call failed", status="UNSUPPORTED")
        raise JobError(f"PayPal error: {err}") from err
