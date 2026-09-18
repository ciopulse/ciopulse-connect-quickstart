"""Lambda wrapper around the ciopulse mock receiver, for an in-account end-to-end test.

Deployed by tools/mock-receiver/template.yaml behind a Lambda function URL.
Enforces the send-a-copy contract exactly like mock_ingest.py (same validate()),
returns 202 with a receipt, and logs metadata only: session id, channel, turn
count, size, whether excluded. Never logs transcript text. Synthetic payloads only.
"""
import base64
import json
import os
import uuid

from mock_ingest import MAX_BYTES, validate

KEY = os.environ.get("MOCK_API_KEY", "")
PATH = "/api/v5/ai-agent/transcripts"


def _resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def handler(event, context):
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "")
    path = event.get("rawPath", "")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    if method == "GET" and path == "/health":
        return _resp(200, {"status": "ok", "contract": ["0.1", "0.2", "0.3"], "mode": "lambda-mock"})
    if method != "POST" or path.rstrip("/") != PATH:
        return _resp(404, {"error": "not found", "hint": f"POST {PATH}"})
    if not KEY or headers.get("x-api-key") != KEY:
        return _resp(401, {"error": "unauthorized", "hint": "send X-API-Key"})

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    if len(raw.encode("utf-8")) > MAX_BYTES:
        return _resp(413, {"error": "payload too large", "limit_bytes": MAX_BYTES})
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _resp(400, {"error": "invalid JSON", "detail": str(exc)})

    problems = validate(payload)
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    if problems:
        print(json.dumps({"event": "rejected", "session_id": sid, "problems": problems}))
        return _resp(400, {"error": "validation failed", "problems": problems})

    receipt = "rcpt_" + uuid.uuid4().hex[:16]
    print(json.dumps({
        "event": "excluded" if payload.get("exclude") else "accepted",
        "receipt_id": receipt, "session_id": sid, "channel": payload.get("channel", "chat"),
        "agent": payload.get("agent"), "turns": len(payload["turns"]), "bytes": len(raw),
        "declared_outcome": payload.get("outcome"), "has_platform_signals": "platform_signals" in payload,
        "contract_version": payload.get("contract_version"), "conversation_id": payload.get("conversation_id", sid),
        "actors": sorted({t.get("actor") for t in payload["turns"] if t.get("actor")}),
        "events": [e.get("type") for e in payload.get("events") or []],
    }))
    if payload.get("exclude"):
        return _resp(202, {"receipt_id": receipt, "status": "excluded"})
    return _resp(202, {"receipt_id": receipt, "status": "accepted"})
