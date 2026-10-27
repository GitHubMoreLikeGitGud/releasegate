"""Generate small test/demo videos with ffmpeg (synthetic pattern + tone). No downloads, no real footage."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _ff(args: list[str]):
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + r.stderr.strip()[-300:])


def _video(secs, size, black: bool = False) -> list[str]:
    src = f"color=c=black:s={size}:d={secs}:r=24" if black else f"testsrc2=s={size}:d={secs}:r=24"
    return ["-f", "lavfi", "-i", src]


_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
          "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]


def _titlecard(secs, size, text) -> list[str]:
    font = next((f for f in _FONTS if Path(f).exists()), None)
    ff = f"fontfile='{font}':" if font else ""
    dt = f"drawtext={ff}text='{text}':fontsize=110:fontcolor=white:x=(w-tw)/2:y=(h-th)/2"
    return ["-f", "lavfi", "-i", f"color=c=0x336699:s={size}:d={secs}:r=24,{dt}"]


def make_sample(path: str | Path, kind: str = "good", secs: float = 8, size: str = "1280x720",
                text: str = "Case Study") -> Path:
    """kind: good | short | silent | black | half_black | lowres | titled (on-screen text, for OCR)"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tone = ["-f", "lavfi", "-i", f"sine=f=440:d={secs}", "-af", "volume=0.8"]
    silent = ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={secs}"]
    enc = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)]
    if kind == "good":
        _ff([*_video(secs, size), *tone, *enc])
    elif kind == "short":
        s = 2
        _ff([*_video(s, size), "-f", "lavfi", "-i", f"sine=f=440:d={s}", "-af", "volume=0.8", *enc])
    elif kind == "silent":
        _ff([*_video(secs, size), *silent, *enc])
    elif kind == "black":
        _ff([*_video(secs, size, black=True), *tone, *enc])
    elif kind == "half_black":
        h = secs / 2
        _ff([*_video(h, size), *_video(h, size, black=True), *tone,
             "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a", *enc])
    elif kind == "titled":
        _ff([*_titlecard(secs, size, text), *tone, *enc])
    elif kind == "lowres":
        _ff([*_video(secs, "320x180"), *tone, *enc])
    else:
        raise ValueError(f"unknown sample kind {kind!r}")
    return path
