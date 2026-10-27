import json

from releasegate import checklist as cl
from releasegate.config import load_config
from conftest import FakeServer


def by_type(checks, t):
    return [c for c in checks if c["type"] == t]


def test_baseline_always_present():
    ids = {c["type"] for c in cl.rules_checklist("make me a video")}
    assert {"audio_present", "loudness", "silence", "black_frames", "resolution", "duration"} <= ids


def test_duration_parsing():
    assert by_type(cl.rules_checklist("between 30 and 45 seconds"), "duration")[0]["params"] == {"min_s": 30.0, "max_s": 45.0}
    assert by_type(cl.rules_checklist("at least 2 minutes"), "duration")[0]["params"] == {"min_s": 120.0}
    assert by_type(cl.rules_checklist("no longer than 90 seconds"), "duration")[0]["params"] == {"max_s": 90.0}
    p = by_type(cl.rules_checklist("a 10 minute video"), "duration")[0]["params"]
    assert p == {"min_s": 510.0, "max_s": 690.0}


def test_resolution_and_loudness_and_text():
    c = cl.rules_checklist('Export in 1080p, -14 LUFS. The end card must say "Thanks for watching".')
    assert by_type(c, "resolution")[0]["params"] == {"min_long_side": 1920, "min_short_side": 1080}
    assert by_type(c, "loudness")[0]["params"] == {"min_lufs": -16.0, "max_lufs": -12.0}
    assert by_type(c, "text_present")[0]["params"]["text"] == "Thanks for watching"


def test_validate_clamps_and_drops_junk():
    raw = [{"type": "duration", "params": {"min_s": 99999999}}, {"type": "rm_rf", "params": {}},
           {"type": "text_present", "params": {"text": "x"}}, {"type": "loudness", "params": {"min_lufs": -5, "max_lufs": -20}},
           "junk", {"type": "silence", "params": {"max_silent_ratio": 7}}, {"type": "audio_present"}]
    out = cl.validate_checklist(raw)
    assert [c["type"] for c in out] == ["duration", "silence", "audio_present"]
    assert out[0]["params"]["min_s"] == 36000 and out[1]["params"]["max_silent_ratio"] == 1


def test_no_key_means_rules(cfg):
    c, src = cl.build_checklist(cfg, "a 5 second video")
    assert src == "rules" and by_type(c, "duration")


def _ai_cfg(tmp_path, url):
    return load_config({"RELEASEGATE_DATA": str(tmp_path / "d"), "ANTHROPIC_API_KEY": "test-key-not-real",
                        "ANTHROPIC_BASE_URL": url})


def test_ai_layer_is_cached_and_merged(tmp_path):
    reply = {"checks": [{"id": "len", "type": "duration", "params": {"min_s": 20, "max_s": 25}, "required": True,
                         "description": "20-25 s"}]}
    srv = FakeServer(lambda m, p, b: (200, {"content": [{"type": "text", "text": json.dumps(reply)}]}))
    try:
        cfg = _ai_cfg(tmp_path, srv.url)
        c1, s1 = cl.build_checklist(cfg, "about 22 seconds long")
        c2, s2 = cl.build_checklist(cfg, "about 22 seconds long")
        assert s1.startswith("ai:") and s2.startswith("ai-cache:")
        assert len(srv.requests) == 1  # second call came from the cache
        r = srv.requests[0]
        assert r["headers"]["x-api-key"] == "test-key-not-real" and r["path"] == "/v1/messages"
        body = json.loads(r["body"])
        assert body["max_tokens"] <= 800 and "haiku" in body["model"]
        assert by_type(c1, "duration")[0]["params"] == {"min_s": 20.0, "max_s": 25.0}
        assert by_type(c1, "black_frames")  # baseline filled in
        assert c1 == c2
    finally:
        srv.close()


def test_ai_failure_degrades_to_rules(tmp_path):
    srv = FakeServer(lambda m, p, b: (500, {"error": "boom"}))
    try:
        c, src = cl.build_checklist(_ai_cfg(tmp_path, srv.url), "at least 30 seconds")
        assert src.startswith("rules") and by_type(c, "duration")[0]["params"] == {"min_s": 30.0}
    finally:
        srv.close()


def test_ai_garbage_reply_degrades_to_rules(tmp_path):
    srv = FakeServer(lambda m, p, b: (200, {"content": [{"type": "text", "text": "sure! here you go"}]}))
    try:
        _, src = cl.build_checklist(_ai_cfg(tmp_path, srv.url), "a video")
        assert src.startswith("rules")
    finally:
        srv.close()


def test_exactly_n_seconds_gets_a_small_tolerance():
    out = cl.validate_checklist([{"type": "duration", "params": {"min_s": 8, "max_s": 8}}])
    assert out[0]["params"] == {"min_s": 7.5, "max_s": 8.5}
    out = cl.validate_checklist([{"type": "duration", "params": {"min_s": 100, "max_s": 100}}])
    assert out[0]["params"] == {"min_s": 95.0, "max_s": 105.0}
