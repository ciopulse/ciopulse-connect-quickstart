"""Handler tests: S3 event -> payload -> 202 from the strict mock receiver, plus negative paths."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import handler
from conftest import (BUCKET, CHAT_KEY, FIXTURES, UNREDACTED_KEY, VOICE_KEY, WAV_KEY,
                      eventbridge_event, load_fixture)


def last_received(mock_ingest):
    rows = mock_ingest["received"]()
    assert rows, "mock receiver logged nothing"
    return rows[-1]


# ----------------------------------------------------------------------------- happy paths

def test_voice_end_to_end_eventbridge(aws, env, mock_ingest, capsys):
    out = handler.lambda_handler(eventbridge_event(VOICE_KEY))
    r = out["results"][0]
    assert r["status"] == "forwarded", r
    assert r["http_status"] == 202 and r["attempts"] == 1
    assert r["contact_id"] == "3f1c9a2e-5b7d-4e8f-9a0b-1c2d3e4f5a6b"
    assert r["channel"] == "voice" and r["turns"] == 8

    rec = last_received(mock_ingest)
    assert rec["session_id"] == "3f1c9a2e-5b7d-4e8f-9a0b-1c2d3e4f5a6b"
    assert rec["channel"] == "voice"
    assert rec["agent"] == {"id": "service-desk-agent", "version": "1.4.2"}
    assert rec["excluded"] is False

    # metrics line: one EMF record, Forwarded=1, and no transcript text anywhere in the logs
    log = capsys.readouterr().out
    emf = json.loads(log.strip().splitlines()[-1])
    assert emf["Forwarded"] == 1 and emf["Failed"] == 0
    assert emf["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "ciopulse/ConnectForwarder"
    assert "VPN" not in log and "certificate" not in log


def test_voice_sends_contract_0_2(aws, env, mock_ingest, monkeypatch):
    captured = {}
    real_post = handler.post_with_retries

    def spy(body, endpoint, key):
        captured["payload"] = json.loads(body)
        captured["key"] = key
        return real_post(body, endpoint, key)

    monkeypatch.setattr(handler, "post_with_retries", spy)
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "forwarded"
    p = captured["payload"]
    assert p["contract_version"] == "0.2"          # the mock rejects voice on 0.1
    assert p["channel"] == "voice"
    assert p["started_at"] == "2026-09-02T00:14:00.000+00:00"
    assert p["ended_at"] == "2026-09-02T00:14:47.000+00:00"
    assert p["outcome"] == "unknown"
    assert "events" not in p
    assert p["platform_signals"]["overall_sentiment_user"] == -0.5
    assert p["metadata"]["queue"] == "General Enquiries"
    assert p["metadata"]["timestamps_estimated"] is False
    assert len(json.dumps(p["metadata"])) <= 2048
    assert captured["key"] == "test-key-123"


def test_chat_end_to_end(aws, env, mock_ingest, monkeypatch):
    captured = {}
    real_post = handler.post_with_retries
    monkeypatch.setattr(handler, "post_with_retries",
                        lambda b, e, k: captured.setdefault("p", json.loads(b)) and real_post(b, e, k))
    r = handler.lambda_handler(eventbridge_event(CHAT_KEY))["results"][0]
    assert r["status"] == "forwarded" and r["channel"] == "chat" and r["turns"] == 5
    p = captured["p"]
    assert p["channel"] == "chat" and p["contract_version"] == "0.2"
    assert p["platform_signals"]["response_time_ms"] == 6200
    assert p["metadata"]["events_skipped"] == 2
    assert last_received(mock_ingest)["channel"] == "chat"


def test_classic_s3_notification_shape_is_url_decoded(aws, env, mock_ingest):
    event = json.loads((FIXTURES / "events" / "s3-notification-voice.json").read_text())
    r = handler.lambda_handler(event)["results"][0]
    assert r["key"] == VOICE_KEY  # %3A decoded back to ':'
    assert r["status"] == "forwarded"


def test_manual_bucket_key_event(aws, env, mock_ingest):
    r = handler.lambda_handler({"bucket": BUCKET, "key": CHAT_KEY})["results"][0]
    assert r["status"] == "forwarded"


def test_secret_fetched_once_and_json_secret_supported(aws, env, mock_ingest):
    aws["secrets"].value = json.dumps({"api_key": "test-key-123"})
    handler.lambda_handler(eventbridge_event(VOICE_KEY))
    handler.lambda_handler(eventbridge_event(CHAT_KEY))
    assert aws["secrets"].calls == 1


# ----------------------------------------------------------------------------- mapping details

def test_outcome_attribute_and_escalation_event(aws, env, mock_ingest, monkeypatch):
    monkeypatch.setenv("OUTCOME_ATTRIBUTE_NAME", "ciopulse_outcome")
    aws["connect"].attributes["3f1c9a2e-5b7d-4e8f-9a0b-1c2d3e4f5a6b"] = {"ciopulse_outcome": "Escalated"}
    aws["connect"].contacts["3f1c9a2e-5b7d-4e8f-9a0b-1c2d3e4f5a6b"]["AgentInfo"] = {
        "Id": "human-1", "ConnectedToAgentTimestamp": handler.datetime(2026, 9, 2, 0, 14, 40, tzinfo=handler.timezone.utc)}
    captured = {}
    real_post = handler.post_with_retries
    monkeypatch.setattr(handler, "post_with_retries",
                        lambda b, e, k: captured.setdefault("p", json.loads(b)) and real_post(b, e, k))
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "forwarded"
    assert captured["p"]["outcome"] == "escalated"
    assert captured["p"]["events"] == [{"type": "escalation_to_human", "ts": "2026-09-02T00:14:40.000+00:00"}]


def test_unknown_outcome_value_becomes_unknown(aws, env, mock_ingest, monkeypatch):
    monkeypatch.setenv("OUTCOME_ATTRIBUTE_NAME", "ciopulse_outcome")
    aws["connect"].attributes["3f1c9a2e-5b7d-4e8f-9a0b-1c2d3e4f5a6b"] = {"ciopulse_outcome": "resolved-ish"}
    captured = {}
    real_post = handler.post_with_retries
    monkeypatch.setattr(handler, "post_with_retries",
                        lambda b, e, k: captured.setdefault("p", json.loads(b)) and real_post(b, e, k))
    handler.lambda_handler(eventbridge_event(VOICE_KEY))
    assert captured["p"]["outcome"] == "unknown"


def test_excluded_queue_sends_stub_without_transcript(aws, env, mock_ingest, monkeypatch):
    monkeypatch.setenv("EXCLUDE_QUEUE_NAMES", "Internal Test, general enquiries")
    captured = {}
    real_post = handler.post_with_retries
    monkeypatch.setattr(handler, "post_with_retries",
                        lambda b, e, k: captured.setdefault("p", json.loads(b)) and real_post(b, e, k))
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "excluded" and r["http_status"] == 202
    assert aws["s3"].calls == []                       # transcript never downloaded
    p = captured["p"]
    assert p["exclude"] is True and p["turns"] == [{"role": "system", "text": "excluded by sender",
                                                    "ts": "2026-09-02T00:14:00.000+00:00"}]
    assert "platform_signals" not in p
    rec = last_received(mock_ingest)
    assert rec["excluded"] is True


def test_contact_metadata_unavailable_estimates_timestamps(aws, env, mock_ingest, monkeypatch):
    aws["connect"].fail = True
    captured = {}
    real_post = handler.post_with_retries
    monkeypatch.setattr(handler, "post_with_retries",
                        lambda b, e, k: captured.setdefault("p", json.loads(b)) and real_post(b, e, k))
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "forwarded" and r["reason"] == "contact_metadata_unavailable"
    p = captured["p"]
    assert p["metadata"]["timestamps_estimated"] is True
    # LastModified 00:17:10 minus 45 s of conversation
    assert p["started_at"] == "2026-09-02T00:16:25.000+00:00"
    assert p["ended_at"] == "2026-09-02T00:17:10.000+00:00"
    assert "queue" not in p["metadata"]


def test_missing_analytics_block_sends_no_platform_signals(aws, env, mock_ingest, monkeypatch):
    doc = load_fixture("contact-lens-voice-redacted.json")
    del doc["ConversationCharacteristics"]
    aws["s3"].put(VOICE_KEY, doc)
    captured = {}
    real_post = handler.post_with_retries
    monkeypatch.setattr(handler, "post_with_retries",
                        lambda b, e, k: captured.setdefault("p", json.loads(b)) and real_post(b, e, k))
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "forwarded"
    assert "platform_signals" not in captured["p"]


# ----------------------------------------------------------------------------- negative paths

@pytest.mark.parametrize("key,reason", [
    (UNREDACTED_KEY, "not_redacted"),
    (WAV_KEY, "not_json"),
    ("connect/example-instance/Analysis/Voice/Redacted/2026/09/02/other.json", "key_pattern"),
])
def test_ineligible_keys_are_skipped_without_reading_s3(aws, env, mock_ingest, key, reason, monkeypatch):
    monkeypatch.setenv("VOICE_KEY_PATTERN", "*Analysis/Voice/Redacted/*_analysis_redacted_*.json")
    r = handler.lambda_handler(eventbridge_event(key))["results"][0]
    assert r["status"] == "skipped" and r["reason"] == reason
    assert aws["s3"].calls == []


def test_more_than_500_turns_is_rejected_not_chunked(aws, env, mock_ingest):
    doc = load_fixture("contact-lens-voice-redacted.json")
    doc["Transcript"] = [{"ParticipantId": "CUSTOMER" if i % 2 else "AGENT", "Content": f"turn {i}",
                          "BeginOffsetMillis": i * 1000} for i in range(600)]
    aws["s3"].put(VOICE_KEY, doc)
    before = len(mock_ingest["received"]())
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "failed" and r["reason"] == "too_many_turns" and r["turns"] == 600
    assert r["http_status"] is None                    # never posted
    assert len(mock_ingest["received"]()) == before


def test_same_speaker_segments_merge_below_cap(aws, env, mock_ingest):
    doc = load_fixture("contact-lens-voice-redacted.json")
    # 600 segments but only 2 speaker changes -> 3 turns after merging
    doc["Transcript"] = [{"ParticipantId": "AGENT" if i < 200 or i >= 400 else "CUSTOMER",
                          "Content": f"segment {i}", "BeginOffsetMillis": i * 50} for i in range(600)]
    aws["s3"].put(VOICE_KEY, doc)
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "forwarded" and r["turns"] == 3


def test_three_participant_contact_skipped_only_when_configured(aws, env, mock_ingest, monkeypatch):
    doc = load_fixture("contact-lens-voice-redacted.json")
    doc["Participants"].append({"ParticipantId": "AGENT-2", "ParticipantRole": "AGENT"})
    doc["Transcript"].append({"ParticipantId": "AGENT-2", "Content": "Network team here.", "BeginOffsetMillis": 44000})
    aws["s3"].put(VOICE_KEY, doc)

    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "forwarded"                  # default: forwarded, with an escalation event

    monkeypatch.setenv("SKIP_MULTI_PARTY_CONTACTS", "true")
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "skipped" and r["reason"] == "multi_party"


def test_unparseable_object_is_a_parse_error(aws, env, mock_ingest, capsys):
    aws["s3"].put(VOICE_KEY, b"not json at all")
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "parse_error"
    emf = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert emf["ParseErrors"] == 1


def test_receiver_400_is_not_retried_and_detail_is_logged(aws, env, mock_ingest, monkeypatch):
    monkeypatch.setenv("AGENT_VERSION", "")             # the mock rejects a missing agent.version
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "failed" and r["http_status"] == 400 and r["attempts"] == 1
    assert "agent.version" in r["receiver_detail"]


def test_bad_api_key_is_401_not_retried(aws, env, mock_ingest):
    aws["secrets"].value = "wrong-key"
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "failed" and r["http_status"] == 401 and r["attempts"] == 1


def test_unrecognised_event_shape_raises(env):
    with pytest.raises(ValueError):
        handler.lambda_handler({"hello": "world"})


# ----------------------------------------------------------------------------- retry behaviour

class _Flaky(BaseHTTPRequestHandler):
    plan: list[int] = []
    hits = 0

    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        _Flaky.hits += 1
        code = _Flaky.plan[min(_Flaky.hits, len(_Flaky.plan)) - 1]
        self.send_response(code)
        if code == 429:
            self.send_header("Retry-After", "1")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


@pytest.fixture
def flaky():
    server = HTTPServer(("127.0.0.1", 0), _Flaky)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Flaky.hits = 0
    yield f"http://127.0.0.1:{server.server_address[1]}/api/v5/ai-agent/transcripts"
    server.shutdown()


def test_retries_5xx_then_succeeds(aws, env, flaky, monkeypatch):
    _Flaky.plan = [503, 502, 202]
    monkeypatch.setenv("CIOPULSE_ENDPOINT", flaky)
    sleeps = []
    monkeypatch.setattr(handler, "SLEEP", sleeps.append)
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "forwarded" and r["attempts"] == 3 and _Flaky.hits == 3
    assert sleeps == [1.0, 3.0]


def test_gives_up_after_three_attempts(aws, env, flaky, monkeypatch, capsys):
    _Flaky.plan = [500]
    monkeypatch.setenv("CIOPULSE_ENDPOINT", flaky)
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "failed" and r["reason"] == "http_500" and r["attempts"] == 3 and _Flaky.hits == 3
    emf = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert emf["Failed"] == 1


def test_429_honours_retry_after(aws, env, flaky, monkeypatch):
    _Flaky.plan = [429, 202]
    monkeypatch.setenv("CIOPULSE_ENDPOINT", flaky)
    sleeps = []
    monkeypatch.setattr(handler, "SLEEP", sleeps.append)
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "forwarded" and r["attempts"] == 2
    assert sleeps == [1.0]


def test_network_error_is_retried_then_failed(aws, env, monkeypatch):
    monkeypatch.setenv("CIOPULSE_ENDPOINT", "http://127.0.0.1:9/api/v5/ai-agent/transcripts")  # nothing listens
    r = handler.lambda_handler(eventbridge_event(VOICE_KEY))["results"][0]
    assert r["status"] == "failed" and r["reason"] == "network" and r["attempts"] == 3 and r["http_status"] == 0
