"""Shared fixtures: stubbed AWS clients, the ciopulse mock receiver in a thread, and env setup."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import threading
from datetime import datetime, timezone
from http.server import HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import handler  # noqa: E402

FIXTURES = ROOT / "fixtures"
VOICE_KEY = ("connect/example-instance/Analysis/Voice/Redacted/2026/09/02/"
             "3f1c9a2e-5b7d-4e8f-9a0b-1c2d3e4f5a6b_analysis_redacted_2026-09-02T00:17:10Z.json")
CHAT_KEY = ("connect/example-instance/Analysis/Chat/Redacted/2026/09/02/"
            "7d2e4f60-8a1b-4c3d-9e5f-0a1b2c3d4e5f_analysis_redacted_2026-09-02T00:24:38Z.json")
UNREDACTED_KEY = ("connect/example-instance/Analysis/Voice/2026/09/02/"
                  "3f1c9a2e-5b7d-4e8f-9a0b-1c2d3e4f5a6b_analysis_2026-09-02T00:17:10Z.json")
WAV_KEY = ("connect/example-instance/Analysis/Voice/Redacted/2026/09/02/"
           "3f1c9a2e-5b7d-4e8f-9a0b-1c2d3e4f5a6b_call_recording_redacted_2026-09-02T00:17:10Z.wav")
BUCKET = "example-connect-bucket"
API_KEY = "test-key-123"

VOICE_START = datetime(2026, 9, 2, 0, 14, 0, tzinfo=timezone.utc)
VOICE_END = datetime(2026, 9, 2, 0, 14, 47, tzinfo=timezone.utc)
CHAT_START = datetime(2026, 9, 2, 0, 20, 0, tzinfo=timezone.utc)
CHAT_END = datetime(2026, 9, 2, 0, 22, 55, tzinfo=timezone.utc)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[str] = []

    def put(self, key: str, doc: dict | bytes):
        self.objects[key] = doc if isinstance(doc, bytes) else json.dumps(doc).encode()

    def get_object(self, Bucket, Key):
        self.calls.append(Key)
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": FakeBody(self.objects[Key]),
                "LastModified": datetime(2026, 9, 2, 0, 17, 10, tzinfo=timezone.utc)}


class FakeSecrets:
    def __init__(self, value: str = API_KEY):
        self.value = value
        self.calls = 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        return {"SecretString": self.value}


class FakeConnect:
    def __init__(self):
        self.contacts: dict[str, dict] = {}
        self.queues: dict[str, str] = {"q-general": "General Enquiries", "q-internal": "Internal Test"}
        self.attributes: dict[str, dict] = {}
        self.fail = False

    def describe_contact(self, InstanceId, ContactId):
        if self.fail:
            raise RuntimeError("simulated DescribeContact failure")
        return {"Contact": self.contacts[ContactId]}

    def describe_queue(self, InstanceId, QueueId):
        return {"Queue": {"Name": self.queues[QueueId]}}

    def get_contact_attributes(self, InstanceId, InitialContactId):
        return {"Attributes": self.attributes.get(InitialContactId, {})}


@pytest.fixture
def aws(monkeypatch):
    s3, secrets, connect = FakeS3(), FakeSecrets(), FakeConnect()
    s3.put(VOICE_KEY, load_fixture("contact-lens-voice-redacted.json"))
    s3.put(CHAT_KEY, load_fixture("contact-lens-chat-redacted.json"))
    connect.contacts["3f1c9a2e-5b7d-4e8f-9a0b-1c2d3e4f5a6b"] = {
        "Id": "3f1c9a2e-5b7d-4e8f-9a0b-1c2d3e4f5a6b", "Channel": "VOICE", "InitiationMethod": "INBOUND",
        "InitiationTimestamp": VOICE_START, "DisconnectTimestamp": VOICE_END,
        "QueueInfo": {"Id": "q-general"},
    }
    connect.contacts["7d2e4f60-8a1b-4c3d-9e5f-0a1b2c3d4e5f"] = {
        "Id": "7d2e4f60-8a1b-4c3d-9e5f-0a1b2c3d4e5f", "Channel": "CHAT", "InitiationMethod": "API",
        "InitiationTimestamp": CHAT_START, "DisconnectTimestamp": CHAT_END,
        "QueueInfo": {"Id": "q-general"},
    }
    handler.CLIENTS.clear()
    handler.CLIENTS.update({"s3": s3, "secretsmanager": secrets, "connect": connect})
    handler._API_KEY_CACHE.clear()
    handler._QUEUE_NAME_CACHE.clear()
    monkeypatch.setattr(handler, "SLEEP", lambda _s: None)
    yield {"s3": s3, "secrets": secrets, "connect": connect}
    handler.CLIENTS.clear()


def _load_mock_module():
    spec = importlib.util.spec_from_file_location("mock_ingest", ROOT / "tools" / "mock-receiver" / "mock_ingest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def mock_ingest(tmp_path_factory):
    """ciopulse's strict conformance receiver, running in a daemon thread."""
    mod = _load_mock_module()
    mod.STATE["key"] = API_KEY
    mod.STATE["log"] = tmp_path_factory.mktemp("mock") / "received.jsonl"
    mod.H.log_message = lambda self, fmt, *a: None  # keep pytest output quiet
    server = HTTPServer(("127.0.0.1", 0), mod.H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield {"module": mod, "url": f"http://127.0.0.1:{port}/api/v5/ai-agent/transcripts",
           "received": lambda: [json.loads(l) for l in mod.STATE["log"].read_text().splitlines() if l.strip()]
           if mod.STATE["log"].exists() else []}
    server.shutdown()


@pytest.fixture
def env(monkeypatch, mock_ingest):
    values = {
        "CIOPULSE_ENDPOINT": mock_ingest["url"],
        "API_KEY_SECRET_ARN": "arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:ciopulse/api-key-AbCdEf",
        "CONNECT_INSTANCE_ARN": "arn:aws:connect:ap-southeast-2:123456789012:instance/11111111-2222-3333-4444-555555555555",
        "AGENT_ID": "service-desk-agent",
        "AGENT_VERSION": "1.4.2",
        "VOICE_KEY_PATTERN": "*Analysis/Voice/Redacted/*.json",
        "CHAT_KEY_PATTERN": "*Analysis/Chat/Redacted/*.json",
        "EXCLUDE_QUEUE_NAMES": "",
        "OUTCOME_ATTRIBUTE_NAME": "",
        "SKIP_MULTI_PARTY_CONTACTS": "false",
        "AGENT_PARTICIPANT_ROLES": "AGENT,BOT,CUSTOM_BOT,SYSTEM",
        "STACK_NAME": "test",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    return values


def eventbridge_event(key: str, bucket: str = BUCKET) -> dict:
    return {"version": "0", "source": "aws.s3", "detail-type": "Object Created",
            "detail": {"bucket": {"name": bucket}, "object": {"key": key, "size": 1}}}
