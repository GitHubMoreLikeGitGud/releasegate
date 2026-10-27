"""Append-only, hash-chained evidence log (one JSON object per line).

Each entry stores the SHA-256 of the previous entry, so editing or deleting an earlier line is detectable
with verify_chain(). It is tamper-evident, not tamper-proof: whoever controls the machine can rewrite it all.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


def _h(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class EvidenceLog:
    def __init__(self, path: Path):
        self.path = Path(path)

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def append(self, event: str, **fields) -> dict:
        prev = self.entries()
        e = {"seq": len(prev) + 1, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields,
             "prev_hash": prev[-1]["hash"] if prev else "0" * 64}
        e["hash"] = _h(e)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(e) + "\n")
        return e

    def verify_chain(self) -> tuple[bool, str]:
        prev = "0" * 64
        for i, e in enumerate(self.entries(), 1):
            if e.get("seq") != i or e.get("prev_hash") != prev or e.get("hash") != _h(e):
                return False, f"chain broken at entry {i}"
            prev = e["hash"]
        return True, "ok"
