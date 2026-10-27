"""Deterministic deliverable checks built on ffprobe / ffmpeg (and tesseract, if installed).

Each check returns a Finding in a "supported / unsupported" style:
  SUPPORTED     the measurement backs the claim
  UNSUPPORTED   the measurement contradicts the claim
  NOT_VERIFIED  the tool needed to test the claim was unavailable or failed (never silently passed)
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path

SUPPORTED = "SUPPORTED"
UNSUPPORTED = "UNSUPPORTED"
NOT_VERIFIED = "NOT_VERIFIED"


@dataclass
class Finding:
    check_id: str
    type: str
    claim: str
    status: str
    measured: dict = field(default_factory=dict)
    expected: dict = field(default_factory=dict)
    method: str = ""
    note: str = ""
    required: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


class ToolError(Exception):
    pass


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
    except FileNotFoundError:
        raise ToolError(f"{cmd[0]} is not installed") from None
    except subprocess.TimeoutExpired:
        raise ToolError(f"{cmd[0]} timed out") from None


def probe(path: str | Path) -> dict:
    """Return container/stream facts from ffprobe."""
    r = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 60)
    if r.returncode != 0:
        raise ToolError("file could not be read as media: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1])
    j = json.loads(r.stdout or "{}")
    streams = j.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    dur = None
    try:
        dur = float(j.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        if v and v.get("duration"):
            dur = float(v["duration"])
    return {
        "duration_s": dur,
        "container": j.get("format", {}).get("format_name"),
        "has_video": v is not None,
        "has_audio": a is not None,
        "width": int(v["width"]) if v else None,
        "height": int(v["height"]) if v else None,
        "video_codec": v.get("codec_name") if v else None,
        "audio_codec": a.get("codec_name") if a else None,
    }


def measure_loudness(path: str | Path) -> float:
    """Integrated loudness in LUFS (EBU R128). -inf for digital silence."""
    r = _run(["ffmpeg", "-nostats", "-hide_banner", "-i", str(path), "-vn", "-af", "ebur128", "-f", "null", "-"])
    out = r.stderr
    tail = out.split("Summary:")[-1] if "Summary:" in out else out
    m = re.search(r"\bI:\s+(-?inf|-?[\d.]+)\s+LUFS", tail)
    if not m:
        raise ToolError("could not read loudness from ffmpeg output")
    return -math.inf if "inf" in m.group(1) else float(m.group(1))


def measure_silence(path: str | Path, duration: float) -> dict:
    r = _run(["ffmpeg", "-nostats", "-hide_banner", "-i", str(path), "-vn",
              "-af", "silencedetect=noise=-50dB:d=1.0", "-f", "null", "-"])
    total = sum(float(x) for x in re.findall(r"silence_duration:\s*([\d.]+)", r.stderr))
    # a file that is silent to the very end only reports start (no duration) on some builds
    starts = re.findall(r"silence_start:\s*(-?[\d.]+)", r.stderr)
    ends = re.findall(r"silence_end:", r.stderr)
    if len(starts) > len(ends) and duration:
        total += max(0.0, duration - max(0.0, float(starts[-1])))
    total = min(total, duration) if duration else total
    return {"silent_s": round(total, 2), "ratio": round(total / duration, 4) if duration else 0.0}


def measure_black(path: str | Path, duration: float) -> dict:
    r = _run(["ffmpeg", "-nostats", "-hide_banner", "-i", str(path), "-an",
              "-vf", "blackdetect=d=0.5:pix_th=0.10", "-f", "null", "-"])
    segs = [float(x) for x in re.findall(r"black_duration:\s*([\d.]+)", r.stderr)]
    total = sum(segs)
    return {"black_s": round(total, 2), "ratio": round(total / duration, 4) if duration else 0.0,
            "longest_s": round(max(segs), 2) if segs else 0.0, "segments": len(segs)}


def ocr_backend() -> str | None:
    """'tesseract' (CLI) or 'rapidocr' (optional pip package rapidocr_onnxruntime), else None."""
    if have("tesseract"):
        return "tesseract"
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return "rapidocr"
    except Exception:
        return None


def ocr_text(path: str | Path, duration: float, frames: int = 12) -> tuple[str, str]:
    """Sample frames and OCR them. Returns (text, backend). Raises ToolError if no OCR backend is available."""
    backend = ocr_backend()
    if backend is None:
        raise ToolError("no OCR backend installed (need the tesseract CLI or the rapidocr_onnxruntime package)")
    out = []
    with tempfile.TemporaryDirectory() as td:
        fps = max(frames / max(duration, 1.0), 0.05)
        r = _run(["ffmpeg", "-nostats", "-hide_banner", "-i", str(path), "-an", "-vf", f"fps={fps:.4f}",
                  "-frames:v", str(frames), f"{td}/f%03d.png"])
        if r.returncode != 0:
            raise ToolError("could not extract frames")
        imgs = sorted(Path(td).glob("f*.png"))
        if backend == "tesseract":
            for img in imgs:
                out.append(_run(["tesseract", str(img), "stdout"], 60).stdout)
        else:
            from rapidocr_onnxruntime import RapidOCR
            eng = RapidOCR()
            for img in imgs:
                res, _ = eng(str(img))
                out.append(" ".join(x[1] for x in (res or [])))
    return "\n".join(out), backend


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def fmt_s(x) -> str:
    return f"{x:.1f}s"


# --------------------------------------------------------------------------- check implementations
def _duration(c, ctx) -> Finding:
    p = c["params"]
    d = ctx["probe"]["duration_s"]
    lo, hi = p.get("min_s"), p.get("max_s")
    exp = {k: v for k, v in (("min_s", lo), ("max_s", hi)) if v is not None}
    claim = c["description"]
    if d is None:
        return Finding(c["id"], c["type"], claim, NOT_VERIFIED, {}, exp, "ffprobe format.duration", "no duration in file")
    ok = (lo is None or d >= lo - 0.25) and (hi is None or d <= hi + 0.25)  # 0.25 s container-rounding tolerance
    return Finding(c["id"], c["type"], claim, SUPPORTED if ok else UNSUPPORTED, {"duration_s": round(d, 2)}, exp,
                   "ffprobe format.duration (±0.25 s tolerance)")


def _resolution(c, ctx) -> Finding:
    p = c["params"]
    pr = ctx["probe"]
    exp = {"min_long_side": p.get("min_long_side"), "min_short_side": p.get("min_short_side")}
    if not pr["has_video"]:
        return Finding(c["id"], c["type"], c["description"], UNSUPPORTED, {"has_video": False}, exp, "ffprobe streams",
                       "no video stream")
    long_, short_ = max(pr["width"], pr["height"]), min(pr["width"], pr["height"])
    ok = long_ >= (p.get("min_long_side") or 0) and short_ >= (p.get("min_short_side") or 0)
    return Finding(c["id"], c["type"], c["description"], SUPPORTED if ok else UNSUPPORTED,
                   {"width": pr["width"], "height": pr["height"]}, exp, "ffprobe video stream size (either orientation)")


def _audio_present(c, ctx) -> Finding:
    ok = ctx["probe"]["has_audio"]
    return Finding(c["id"], c["type"], c["description"], SUPPORTED if ok else UNSUPPORTED,
                   {"has_audio": ok}, {"has_audio": True}, "ffprobe streams")


def _loudness(c, ctx) -> Finding:
    p = c["params"]
    exp = {"min_lufs": p.get("min_lufs"), "max_lufs": p.get("max_lufs")}
    if not ctx["probe"]["has_audio"]:
        return Finding(c["id"], c["type"], c["description"], UNSUPPORTED, {"has_audio": False}, exp, "ffmpeg ebur128",
                       "no audio stream to measure")
    try:
        l = measure_loudness(ctx["path"])
    except ToolError as e:
        return Finding(c["id"], c["type"], c["description"], NOT_VERIFIED, {}, exp, "ffmpeg ebur128", str(e))
    ok = p.get("min_lufs", -math.inf) <= l <= p.get("max_lufs", math.inf)
    return Finding(c["id"], c["type"], c["description"], SUPPORTED if ok else UNSUPPORTED,
                   {"integrated_lufs": None if math.isinf(l) else round(l, 1), "silent": math.isinf(l)}, exp,
                   "ffmpeg ebur128 integrated loudness")


def _silence(c, ctx) -> Finding:
    p = c["params"]
    exp = {"max_silent_ratio": p.get("max_silent_ratio")}
    if not ctx["probe"]["has_audio"]:
        return Finding(c["id"], c["type"], c["description"], UNSUPPORTED, {"has_audio": False}, exp, "ffmpeg silencedetect",
                       "no audio stream")
    try:
        m = measure_silence(ctx["path"], ctx["probe"]["duration_s"] or 0.0)
    except ToolError as e:
        return Finding(c["id"], c["type"], c["description"], NOT_VERIFIED, {}, exp, "ffmpeg silencedetect", str(e))
    ok = m["ratio"] <= p.get("max_silent_ratio", 0.2)
    return Finding(c["id"], c["type"], c["description"], SUPPORTED if ok else UNSUPPORTED, m, exp,
                   "ffmpeg silencedetect (< -50 dB for >= 1 s)")


def _black(c, ctx) -> Finding:
    p = c["params"]
    exp = {"max_black_ratio": p.get("max_black_ratio"), "max_single_s": p.get("max_single_s")}
    if not ctx["probe"]["has_video"]:
        return Finding(c["id"], c["type"], c["description"], UNSUPPORTED, {"has_video": False}, exp, "ffmpeg blackdetect",
                       "no video stream")
    try:
        m = measure_black(ctx["path"], ctx["probe"]["duration_s"] or 0.0)
    except ToolError as e:
        return Finding(c["id"], c["type"], c["description"], NOT_VERIFIED, {}, exp, "ffmpeg blackdetect", str(e))
    ok = m["ratio"] <= p.get("max_black_ratio", 0.05) and m["longest_s"] <= p.get("max_single_s", 2.0)
    return Finding(c["id"], c["type"], c["description"], SUPPORTED if ok else UNSUPPORTED, m, exp,
                   "ffmpeg blackdetect (>= 0.5 s runs of near-black frames)")


def _text_present(c, ctx) -> Finding:
    want = c["params"]["text"]
    exp = {"text": want}
    if not ctx["probe"]["has_video"]:
        return Finding(c["id"], c["type"], c["description"], UNSUPPORTED, {"has_video": False}, exp, "OCR",
                       "no video stream")
    try:
        found, backend = ocr_text(ctx["path"], ctx["probe"]["duration_s"] or 1.0)
    except ToolError as e:
        return Finding(c["id"], c["type"], c["description"], NOT_VERIFIED, {}, exp, "OCR on 12 sampled frames",
                       f"{e}; this claim was NOT checked")
    ok = _norm_text(want) in _norm_text(found)
    return Finding(c["id"], c["type"], c["description"], SUPPORTED if ok else UNSUPPORTED,
                   {"found": ok, "ocr_chars": len(found)}, exp, f"{backend} OCR on 12 sampled frames",
                   "" if ok else "text not seen in sampled frames; short on-screen text can be missed")


CHECKS = {
    "duration": _duration,
    "resolution": _resolution,
    "audio_present": _audio_present,
    "loudness": _loudness,
    "silence": _silence,
    "black_frames": _black,
    "text_present": _text_present,
}


def run_checklist(path: str | Path, checklist: list[dict]) -> list[Finding]:
    """Run every check in the checklist against the file. Never raises for a bad file: it reports it."""
    try:
        pr = probe(path)
    except ToolError as e:
        return [Finding(c["id"], c["type"], c["description"], UNSUPPORTED, {}, {}, "ffprobe", str(e),
                        required=c.get("required", True)) for c in checklist]
    ctx = {"path": str(path), "probe": pr}
    out = []
    for c in checklist:
        fn = CHECKS.get(c["type"])
        if fn is None:
            f = Finding(c["id"], c["type"], c["description"], NOT_VERIFIED, {}, {}, "", f"unknown check type {c['type']!r}")
        else:
            f = fn(c, ctx)
        f.required = c.get("required", True)
        out.append(f)
    return out


def decide(findings: list[Finding]) -> tuple[str, str]:
    """CAPTURE only if every required claim is SUPPORTED. VOID if any is UNSUPPORTED. HOLD if some were not verified."""
    req = [f for f in findings if f.required]
    bad = [f for f in req if f.status == UNSUPPORTED]
    unk = [f for f in req if f.status == NOT_VERIFIED]
    if bad:
        return "VOID", f"{len(bad)} required claim(s) unsupported: " + ", ".join(f.check_id for f in bad)
    if unk:
        return "HOLD", f"{len(unk)} required claim(s) could not be verified: " + ", ".join(f.check_id for f in unk)
    return "CAPTURE", f"all {len(req)} required claim(s) supported"
