"""ciopulse Connect quickstart: S3 -> Lambda -> send-a-copy forwarder.

Triggered by S3 "Object Created" events (via EventBridge, or the classic S3
notification shape) for redacted Amazon Connect conversational-analytics
files. Maps each file to a send-a-copy v0.2 payload and POSTs it to ciopulse.

Privacy rules enforced here, not just documented:
  * Only keys with "/Redacted/" that match the configured glob are read.
  * Transcript text is never logged. Log lines carry ContactId, sizes, HTTP
    status codes, attempt counts and timings only.
  * The API key comes from Secrets Manager at runtime and is cached in memory.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional
from urllib.parse import unquote_plus

import boto3

from parsers.chat import parse_chat
from parsers.common import ROLE_SYSTEM, ParsedTranscript, iso, parse_agent_roles
from parsers.voice import parse_voice

FORWARDER_VERSION = "0.1.0"
CONTRACT_VERSION = "0.2"
MAX_TURNS = 500
MAX_PAYLOAD_BYTES = 1_048_576
MAX_METADATA_BYTES = 2048
DELIVERY_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 3.0)
HTTP_TIMEOUT_SECONDS = 15
OUTCOMES = {"contained", "escalated", "abandoned", "unknown"}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

log = logging.getLogger("ciopulse.forwarder")
log.setLevel(logging.INFO)

# Injection points for tests. Production leaves these alone.
CLIENTS: dict = {}
SLEEP = time.sleep
_API_KEY_CACHE: dict = {}
_QUEUE_NAME_CACHE: dict = {}


# --------------------------------------------------------------------------- config

class Settings:
    def __init__(self) -> None:
        env = os.environ.get
        self.endpoint = env("CIOPULSE_ENDPOINT", "https://app.cio-pulse.com/api/v5/ai-agent/transcripts")
        self.secret_arn = env("API_KEY_SECRET_ARN", "")
        self.instance_arn = env("CONNECT_INSTANCE_ARN", "")
        self.agent_id = env("AGENT_ID", "")
        self.agent_version = env("AGENT_VERSION", "")
        self.voice_pattern = env("VOICE_KEY_PATTERN", "*Analysis/Voice/Redacted/*.json")
        self.chat_pattern = env("CHAT_KEY_PATTERN", "*Analysis/Chat/Redacted/*.json")
        self.exclude_queues = {q.strip().lower() for q in env("EXCLUDE_QUEUE_NAMES", "").split(",") if q.strip()}
        self.outcome_attribute = env("OUTCOME_ATTRIBUTE_NAME", "").strip()
        self.skip_multi_party = env("SKIP_MULTI_PARTY_CONTACTS", "false").strip().lower() == "true"
        self.agent_roles = parse_agent_roles(env("AGENT_PARTICIPANT_ROLES", ""))
        self.namespace = env("METRIC_NAMESPACE", "ciopulse/ConnectForwarder")
        self.stack = env("STACK_NAME", "local")

    @property
    def instance_id(self) -> str:
        return self.instance_arn.rsplit("/", 1)[-1] if self.instance_arn else ""


def client(name: str):
    if name not in CLIENTS:
        CLIENTS[name] = boto3.client(name)
    return CLIENTS[name]


def api_key(secret_arn: str) -> str:
    if secret_arn in _API_KEY_CACHE:
        return _API_KEY_CACHE[secret_arn]
    resp = client("secretsmanager").get_secret_value(SecretId=secret_arn)
    raw = resp.get("SecretString")
    if raw is None:
        import base64
        raw = base64.b64decode(resp["SecretBinary"]).decode("utf-8")
    key = raw.strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            key = str(obj.get("api_key") or obj.get("apiKey") or obj.get("key") or next(iter(obj.values())))
    except ValueError:
        pass
    _API_KEY_CACHE[secret_arn] = key
    return key


# --------------------------------------------------------------------------- events

def iter_objects(event: dict) -> Iterator[tuple[str, str]]:
    """Yield (bucket, key) for every supported event shape."""
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")
    if "Records" in event:  # classic S3 notification (also what `sam local invoke` fixtures use)
        for rec in event["Records"]:
            s3 = rec.get("s3") or {}
            bucket = (s3.get("bucket") or {}).get("name")
            key = (s3.get("object") or {}).get("key")
            if bucket and key:
                yield bucket, unquote_plus(key)
        return
    if event.get("source") == "aws.s3" and isinstance(event.get("detail"), dict):  # EventBridge
        d = event["detail"]
        yield d["bucket"]["name"], d["object"]["key"]
        return
    if "bucket" in event and "key" in event:  # manual invocation
        yield event["bucket"], event["key"]
        return
    raise ValueError("unrecognised event shape; expected S3 notification, EventBridge Object Created, or {bucket,key}")


def classify_key(key: str, s: Settings) -> tuple[Optional[str], Optional[str]]:
    """Return (channel, skip_reason). Channel is 'voice' or 'chat' when eligible."""
    if "/Redacted/" not in key and not key.startswith("Redacted/"):
        return None, "not_redacted"
    if not key.lower().endswith(".json"):
        return None, "not_json"
    if fnmatch.fnmatchcase(key, s.voice_pattern):
        return "voice", None
    if fnmatch.fnmatchcase(key, s.chat_pattern):
        return "chat", None
    return None, "key_pattern"


def contact_id_from_key(key: str) -> Optional[str]:
    name = key.rsplit("/", 1)[-1]
    candidate = name.split("_", 1)[0]
    return candidate if UUID_RE.match(candidate) else None


# --------------------------------------------------------------------------- contact metadata

def contact_metadata(contact_id: str, s: Settings) -> Optional[dict]:
    """Read-only Connect lookups. Returns None when the instance is not configured or the call fails."""
    if not s.instance_id:
        return None
    try:
        c = client("connect").describe_contact(InstanceId=s.instance_id, ContactId=contact_id)["Contact"]
    except Exception as exc:  # noqa: BLE001 - we log the class only, never the payload
        log.warning(json.dumps({"event": "describe_contact_failed", "contact_id": contact_id,
                                "error": type(exc).__name__}))
        return None
    meta = {
        "started_at": c.get("InitiationTimestamp"),
        "ended_at": c.get("DisconnectTimestamp"),
        "queue_id": (c.get("QueueInfo") or {}).get("Id"),
        "queue_name": None,
        "agent_connected_at": (c.get("AgentInfo") or {}).get("ConnectedToAgentTimestamp"),
        "initiation_method": c.get("InitiationMethod"),
        "attributes": {},
    }
    if meta["queue_id"]:
        meta["queue_name"] = queue_name(meta["queue_id"], s)
    if s.outcome_attribute:
        try:
            attrs = client("connect").get_contact_attributes(InstanceId=s.instance_id,
                                                             InitialContactId=contact_id)
            meta["attributes"] = attrs.get("Attributes") or {}
        except Exception as exc:  # noqa: BLE001
            log.warning(json.dumps({"event": "get_contact_attributes_failed", "contact_id": contact_id,
                                    "error": type(exc).__name__}))
    return meta


def queue_name(queue_id: str, s: Settings) -> Optional[str]:
    if queue_id in _QUEUE_NAME_CACHE:
        return _QUEUE_NAME_CACHE[queue_id]
    try:
        name = client("connect").describe_queue(InstanceId=s.instance_id, QueueId=queue_id)["Queue"]["Name"]
    except Exception as exc:  # noqa: BLE001
        log.warning(json.dumps({"event": "describe_queue_failed", "queue_id": queue_id,
                                "error": type(exc).__name__}))
        name = None
    _QUEUE_NAME_CACHE[queue_id] = name
    return name


def outcome_from(meta: Optional[dict], s: Settings) -> str:
    if not s.outcome_attribute or not meta:
        return "unknown"
    value = str((meta.get("attributes") or {}).get(s.outcome_attribute, "")).strip().lower()
    return value if value in OUTCOMES else "unknown"


# --------------------------------------------------------------------------- payload

def build_payload(contact_id: str, channel: str, started_at: datetime, ended_at: datetime,
                  parsed: ParsedTranscript, meta: Optional[dict], s: Settings,
                  timestamps_estimated: bool) -> dict:
    payload = {
        "contract_version": CONTRACT_VERSION,
        "session_id": contact_id,
        "agent": {"id": s.agent_id, "version": s.agent_version},
        "channel": channel,
        "started_at": iso(started_at),
        "ended_at": iso(ended_at),
        "outcome": outcome_from(meta, s),
        "turns": parsed.turns,
    }
    events = []
    if meta and meta.get("agent_connected_at"):
        events.append({"type": "escalation_to_human", "ts": iso(meta["agent_connected_at"])})
    elif parsed.escalation_at:
        events.append({"type": "escalation_to_human", "ts": iso(parsed.escalation_at)})
    if events:
        payload["events"] = events
    if parsed.platform_signals:
        payload["platform_signals"] = parsed.platform_signals

    metadata = {
        "source": "amazon-connect",
        "forwarder": f"ciopulse-connect-quickstart/{FORWARDER_VERSION}",
        "timestamps_estimated": timestamps_estimated,
    }
    if meta:
        if meta.get("queue_name"):
            metadata["queue"] = str(meta["queue_name"])[:200]
        if meta.get("initiation_method"):
            metadata["initiation_method"] = str(meta["initiation_method"])[:50]
    for k, v in parsed.notes.items():
        if v:
            metadata[k] = v
    while len(json.dumps(metadata)) > MAX_METADATA_BYTES and len(metadata) > 1:
        metadata.pop(next(reversed(metadata)))
    payload["metadata"] = metadata
    return payload


def build_excluded_payload(contact_id: str, channel: str, started_at: datetime, ended_at: datetime,
                           reason: str, s: Settings) -> dict:
    """The stub sent for excluded contacts: envelope only, no transcript text leaves the account."""
    return {
        "contract_version": CONTRACT_VERSION,
        "session_id": contact_id,
        "agent": {"id": s.agent_id, "version": s.agent_version},
        "channel": channel,
        "started_at": iso(started_at),
        "ended_at": iso(ended_at),
        "exclude": True,
        "turns": [{"role": ROLE_SYSTEM, "text": "excluded by sender", "ts": iso(started_at)}],
        "metadata": {"source": "amazon-connect", "forwarder": f"ciopulse-connect-quickstart/{FORWARDER_VERSION}",
                     "exclude_reason": reason},
    }


# --------------------------------------------------------------------------- delivery

def post_with_retries(body: bytes, endpoint: str, key: str) -> tuple[int, int, Optional[str]]:
    """POST with up to DELIVERY_ATTEMPTS tries on 429/5xx/network errors.

    Returns (http_status, attempts, error_detail). http_status is 0 when no
    HTTP response was ever received. error_detail is the receiver's error body
    for 4xx responses (field-level problems, never our payload).
    """
    status, detail = 0, None
    for attempt in range(1, DELIVERY_ATTEMPTS + 1):
        req = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json", "X-API-Key": key,
                     "User-Agent": f"ciopulse-connect-quickstart/{FORWARDER_VERSION}"})
        retry_after = None
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                return resp.status, attempt, None
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status == 429 or status >= 500:
                retry_after = exc.headers.get("Retry-After")
            else:
                try:
                    detail = exc.read(4096).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    detail = None
                return status, attempt, detail
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            status = 0
        if attempt < DELIVERY_ATTEMPTS:
            delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
            if retry_after and str(retry_after).isdigit():
                delay = max(delay, min(int(retry_after), 30))
            SLEEP(delay)
    return status, DELIVERY_ATTEMPTS, detail


# --------------------------------------------------------------------------- core

def process_object(bucket: str, key: str, s: Settings) -> dict:
    t0 = time.monotonic()
    result = {"bucket": bucket, "key": key, "status": "skipped", "reason": None,
              "contact_id": contact_id_from_key(key), "channel": None,
              "bytes_in": 0, "bytes_out": 0, "turns": 0, "http_status": None, "attempts": 0}

    channel, skip = classify_key(key, s)
    if skip:
        result["reason"] = skip
        return _finish(result, t0)
    result["channel"] = channel

    contact_id = result["contact_id"]
    meta = contact_metadata(contact_id, s) if contact_id else None

    # Queue exclusion is decided before the transcript is even downloaded.
    if meta and meta.get("queue_name") and meta["queue_name"].strip().lower() in s.exclude_queues:
        started_at = meta.get("started_at") or datetime.now(timezone.utc)
        ended_at = meta.get("ended_at") or started_at
        payload = build_excluded_payload(contact_id, channel, started_at, ended_at, "queue", s)
        return _deliver(payload, result, s, t0, excluded=True)

    try:
        obj = client("s3").get_object(Bucket=bucket, Key=key)
        raw = obj["Body"].read()
        last_modified = obj.get("LastModified") or datetime.now(timezone.utc)
    except Exception as exc:  # noqa: BLE001
        result.update(status="failed", reason="s3_get_failed", error=type(exc).__name__)
        return _finish(result, t0)
    result["bytes_in"] = len(raw)

    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ValueError("analysis file is not a JSON object")
        if not contact_id:
            contact_id = str((doc.get("CustomerMetadata") or {}).get("ContactId") or "")
            result["contact_id"] = contact_id or None
            if not contact_id:
                raise ValueError("no ContactId in key or CustomerMetadata")
            meta = contact_metadata(contact_id, s)
        if meta is None:
            result["reason"] = "contact_metadata_unavailable"

        participants = doc.get("Participants") or []
        if s.skip_multi_party and len(participants) > 2:
            result.update(status="skipped", reason="multi_party")
            return _finish(result, t0)

        timestamps_estimated = not (meta and meta.get("started_at"))
        started_at = (meta or {}).get("started_at")
        if started_at is None:
            duration = ((doc.get("ConversationCharacteristics") or {}).get("TotalConversationDurationMillis") or 0)
            started_at = last_modified - timedelta(milliseconds=int(duration))

        if channel == "voice":
            # Voice offsets count from when analytics started, which is the moment the call
            # connected to an agent (verified on a live instance), not from contact initiation.
            anchor = (meta or {}).get("agent_connected_at") or started_at
            parsed = parse_voice(doc, anchor, s.agent_roles)
            parsed.notes["offset_anchor"] = "agent_connected" if (meta or {}).get("agent_connected_at") else "contact_start"
        else:
            parsed = parse_chat(doc, started_at, s.agent_roles)

        ended_at = (meta or {}).get("ended_at")
        if ended_at is None:
            if parsed.duration_ms:
                ended_at = started_at + timedelta(milliseconds=parsed.duration_ms)
            elif parsed.turns:
                ended_at = datetime.fromisoformat(parsed.turns[-1]["ts"])
            else:
                ended_at = started_at
    except Exception as exc:  # noqa: BLE001
        result.update(status="parse_error", reason="parse_error", error=type(exc).__name__)
        return _finish(result, t0)

    result["turns"] = len(parsed.turns)
    if not parsed.turns:
        result.update(status="failed", reason="no_turns")
        return _finish(result, t0)
    if len(parsed.turns) > MAX_TURNS:
        result.update(status="failed", reason="too_many_turns")
        return _finish(result, t0)

    payload = build_payload(contact_id, channel, started_at, ended_at, parsed, meta, s, timestamps_estimated)
    return _deliver(payload, result, s, t0, excluded=False)


def _deliver(payload: dict, result: dict, s: Settings, t0: float, excluded: bool) -> dict:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    result["bytes_out"] = len(body)
    result["turns"] = len(payload.get("turns") or [])
    if len(body) > MAX_PAYLOAD_BYTES:
        result.update(status="failed", reason="payload_too_large")
        return _finish(result, t0)
    try:
        key = api_key(s.secret_arn)
    except Exception as exc:  # noqa: BLE001
        result.update(status="failed", reason="secret_unavailable", error=type(exc).__name__)
        return _finish(result, t0)

    status, attempts, detail = post_with_retries(body, s.endpoint, key)
    result.update(http_status=status, attempts=attempts)
    if status == 202 or status == 200:
        result["status"] = "excluded" if excluded else "forwarded"
    else:
        result.update(status="failed", reason=f"http_{status}" if status else "network")
        if detail:
            result["receiver_detail"] = detail[:1000]
    return _finish(result, t0)


def _finish(result: dict, t0: float) -> dict:
    result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    return result


def emit(result: dict, s: Settings) -> None:
    """One log line per object: a CloudWatch embedded-metric-format record. No transcript text."""
    counts = {
        "Forwarded": int(result["status"] == "forwarded"),
        "Excluded": int(result["status"] == "excluded"),
        "Failed": int(result["status"] == "failed"),
        "ParseErrors": int(result["status"] == "parse_error"),
        "Skipped": int(result["status"] == "skipped"),
    }
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": s.namespace,
                "Dimensions": [["Stack"]],
                "Metrics": [{"Name": n, "Unit": "Count"} for n in counts],
            }],
        },
        "Stack": s.stack,
        **counts,
        **{k: v for k, v in result.items() if k not in ("bucket",)},
    }
    print(json.dumps(record))


def lambda_handler(event, context=None):
    s = Settings()
    results = []
    for bucket, key in iter_objects(event):
        result = process_object(bucket, key, s)
        emit(result, s)
        results.append(result)
    return {"processed": len(results), "results": results}
