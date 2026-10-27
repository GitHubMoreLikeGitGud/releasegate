"""Checker tests on small ffmpeg-generated videos: a good file passes; too-short/silent/black/low-res fail."""
import math

from releasegate import checks as ck
from releasegate.checklist import rules_checklist

BRIEF = "Deliver an 8 second video, at least 720p."


def run(path, brief=BRIEF):
    fs = ck.run_checklist(path, rules_checklist(brief))
    return {f.check_id: f for f in fs}, ck.decide(fs)


def test_good_file_passes_everything(samples):
    by, (decision, _) = run(samples["good"])
    assert decision == "CAPTURE"
    assert all(f.status == ck.SUPPORTED for f in by.values())
    assert 7.9 <= by["duration"].measured["duration_s"] <= 8.1
    assert -30 <= by["loudness"].measured["integrated_lufs"] <= -9


def test_too_short_fails_duration_only(samples):
    by, (decision, reason) = run(samples["short"])
    assert decision == "VOID" and "duration" in reason
    assert by["duration"].status == ck.UNSUPPORTED and by["duration"].measured["duration_s"] < 3
    assert {k for k, f in by.items() if f.status != ck.SUPPORTED} == {"duration"}


def test_silent_file_fails_loudness_and_silence(samples):
    by, (decision, _) = run(samples["silent"])
    assert decision == "VOID"
    assert by["silence"].status == ck.UNSUPPORTED and by["silence"].measured["ratio"] > 0.9
    assert by["loudness"].status == ck.UNSUPPORTED


def test_all_black_fails_black_frames(samples):
    by, (decision, _) = run(samples["black"])
    assert decision == "VOID"
    assert by["black"].status == ck.UNSUPPORTED and by["black"].measured["ratio"] > 0.9


def test_half_black_fails_black_frames(samples):
    by, (decision, _) = run(samples["half_black"])
    assert decision == "VOID"
    assert 0.4 < by["black"].measured["ratio"] < 0.6 and by["black"].measured["longest_s"] > 3


def test_low_resolution_fails(samples):
    by, (decision, _) = run(samples["lowres"])
    assert decision == "VOID" and by["resolution"].status == ck.UNSUPPORTED
    assert by["resolution"].measured == {"width": 320, "height": 180}


def test_vertical_video_accepted_for_1080p_request(tmp_path):
    from releasegate.samples import make_sample
    p = make_sample(tmp_path / "v.mp4", "good", secs=3, size="1080x1920")
    by, _ = run(p, "A 3 second 1080p video")
    assert by["resolution"].status == ck.SUPPORTED


def test_not_a_media_file_fails_cleanly(tmp_path):
    p = tmp_path / "x.mp4"
    p.write_text("this is not a video")
    fs = ck.run_checklist(p, rules_checklist(BRIEF))
    assert ck.decide(fs)[0] == "VOID"
    assert all(f.status == ck.UNSUPPORTED for f in fs)


def test_audio_missing_fails(tmp_path):
    import subprocess
    p = tmp_path / "noaudio.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:d=8:r=24",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)], check=True)
    by, (decision, _) = run(p)
    assert decision == "VOID" and by["audio"].status == ck.UNSUPPORTED


def test_measure_loudness_silence_is_minus_inf_or_floor(samples):
    l = ck.measure_loudness(samples["silent"])
    assert l == -math.inf or l <= -69


def test_text_check_with_ocr(samples):
    if ck.ocr_backend() is None:
        import pytest
        pytest.skip("no OCR backend installed")
    cl = rules_checklist('An 8 second 720p video. The title card must say "Case Study".')
    by = {f.check_id: f for f in ck.run_checklist(samples["titled"], cl)}
    assert by["text1"].status == ck.SUPPORTED
    cl = rules_checklist('An 8 second 720p video. The title card must say "Annual Budget".')
    by = {f.check_id: f for f in ck.run_checklist(samples["titled"], cl)}
    assert by["text1"].status == ck.UNSUPPORTED


def test_text_check_without_ocr_is_not_verified_never_a_silent_pass(samples, monkeypatch):
    monkeypatch.setattr(ck, "ocr_backend", lambda: None)
    cl = rules_checklist('An 8 second 720p video. The title card must say "Case Study".')
    fs = ck.run_checklist(samples["titled"], cl)
    by = {f.check_id: f for f in fs}
    assert by["text1"].status == ck.NOT_VERIFIED
    assert ck.decide(fs)[0] == "HOLD"


def test_optional_unsupported_check_does_not_block_capture():
    f1 = ck.Finding("a", "duration", "x", ck.SUPPORTED)
    f2 = ck.Finding("b", "text_present", "y", ck.UNSUPPORTED, required=False)
    assert ck.decide([f1, f2])[0] == "CAPTURE"
