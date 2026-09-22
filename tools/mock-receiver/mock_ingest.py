#!/usr/bin/env python3
"""
Mock ingest endpoint — send-a-copy contract, reference implementation.

    python3 mock_ingest.py                 # listens on :8088
    python3 mock_ingest.py --port 9000 --key testkey123

Stdlib only. No pip install, no AWS, no ciopulse infrastructure — it exists so a
forwarder can be built and proven against the contract before the real endpoint
is live. Synthetic transcripts only; this stores what it receives in plain text
and must never see a real conversation.

    POST /api/v5/ai-agent/transcripts     the contract
    GET  /received                        what it has accepted, newest first
    GET  /health

Auth: X-API-Key: <key>

It enforces the contract strictly and returns field-level errors, so a 202 here
means the payload really is conformant — that is the whole point of it.
"""
import argparse, json, re, uuid, sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

MAX_BYTES = 1_048_576
MAX_TURNS = 500
MAX_META = 2048
ROLES = {"user", "agent", "system"}
ACTORS = {"bot", "human"}
CHANNELS = {"chat", "voice"}
OUTCOMES = {"contained", "escalated", "abandoned", "unknown"}
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)$")

# "seen" is keyed on session_id alone because this mock serves a single sender. The real endpoint keys
# idempotency on (tenant, session_id): two customers can legitimately send the same session_id. Do not
# port this single-key logic into the production endpoint.
STATE = {"key": "testkey123", "log": Path("received.jsonl"), "seen": {}}


def iso(v):
    return isinstance(v, str) and bool(ISO.match(v))


def validate(p):
    """Return a list of {field, error}. Empty list = conformant."""
    e = []

    def bad(f, m):
        e.append({"field": f, "error": m})

    if not isinstance(p, dict):
        return [{"field": "<root>", "error": "payload must be a JSON object"}]

    cv = p.get("contract_version")
    if cv is None:
        bad("contract_version", "required")
    elif cv not in ("0.1", "0.2", "0.3"):
        bad("contract_version", f"unsupported version {cv!r}; this endpoint accepts 0.1, 0.2 or 0.3")

    sid = p.get("session_id")
    if not sid:
        bad("session_id", "required")
    elif not isinstance(sid, str) or not (1 <= len(sid) <= 50):
        bad("session_id", "must be a string of 1-50 characters (it is also the survey tid)")

    conv = p.get("conversation_id")
    if conv is not None and (not isinstance(conv, str) or not (1 <= len(conv) <= 50)):
        bad("conversation_id", "must be a string of 1-50 characters (it is the survey tid)")

    ag = p.get("agent")
    if not isinstance(ag, dict):
        bad("agent", "required object with id and version")
    else:
        if not ag.get("id"):
            bad("agent.id", "required")
        if not ag.get("version"):
            bad("agent.version", "required — per-version scorecards depend on it")

    for f in ("started_at", "ended_at"):
        if f not in p:
            bad(f, "required")
        elif not iso(p[f]):
            bad(f, "must be ISO 8601 with timezone, e.g. 2026-09-02T08:14:02+08:00")

    ch = p.get("channel")
    if ch is not None and ch not in CHANNELS:
        bad("channel", f"must be one of {sorted(CHANNELS)}")
    if ch == "voice" and cv == "0.1":
        bad("channel", "voice requires contract_version 0.2")

    oc = p.get("outcome")
    if oc is not None and oc not in OUTCOMES:
        bad("outcome", f"must be one of {sorted(OUTCOMES)}")

    turns = p.get("turns")
    if not isinstance(turns, list) or not turns:
        bad("turns", "required, non-empty array")
    elif len(turns) > MAX_TURNS:
        bad("turns", f"{len(turns)} turns exceeds the {MAX_TURNS} limit")
    else:
        last = None
        for i, t in enumerate(turns):
            if not isinstance(t, dict):
                bad(f"turns[{i}]", "must be an object")
                continue
            if t.get("role") not in ROLES:
                bad(f"turns[{i}].role", f"must be one of {sorted(ROLES)}")
            if "actor" in t and t["actor"] not in ACTORS:
                bad(f"turns[{i}].actor", f"must be one of {sorted(ACTORS)} when present")
            if "actor" in t and t.get("role") != "agent":
                bad(f"turns[{i}].actor", "only meaningful on agent turns")
            if not isinstance(t.get("text"), str):
                bad(f"turns[{i}].text", "required string")
            ts = t.get("ts")
            if not iso(ts):
                bad(f"turns[{i}].ts", "must be ISO 8601 with timezone")
            elif last and ts < last:
                bad(f"turns[{i}].ts", "turns must be in chronological order")
            if iso(ts):
                last = ts

    md = p.get("metadata")
    if md is not None:
        if not isinstance(md, dict):
            bad("metadata", "must be an object")
        elif len(json.dumps(md)) > MAX_META:
            bad("metadata", f"exceeds {MAX_META} bytes")

    if "exclude" in p and not isinstance(p["exclude"], bool):
        bad("exclude", "must be boolean")

    ev = p.get("events")
    if ev is not None:
        if not isinstance(ev, list):
            bad("events", "must be an array")
        else:
            for i, x in enumerate(ev):
                if not isinstance(x, dict) or "type" not in x:
                    bad(f"events[{i}]", "each event needs a type")
                    continue
                if x["type"] == "escalation_to_human" and "ts" not in x:
                    bad(f"events[{i}].ts", "escalation_to_human requires ts — the bot/human turn split "
                                           "falls back to this timestamp when turns[].actor is absent")
                elif "ts" in x and not iso(x["ts"]):
                    bad(f"events[{i}].ts", "must be ISO 8601 with timezone")

    return e


class H(BaseHTTPRequestHandler):
    server_version = "ciopulse-mock-ingest/0.3"

    def log_message(self, fmt, *a):
        sys.stderr.write("  %s\n" % (fmt % a))

    def _send(self, code, body):
        b = json.dumps(body, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        return self.headers.get("X-API-Key") == STATE["key"]

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok", "contract": ["0.1", "0.2", "0.3"]})
        if self.path == "/received":
            rows = []
            if STATE["log"].exists():
                rows = [json.loads(l) for l in STATE["log"].read_text().splitlines() if l.strip()]
            return self._send(200, {"count": len(rows), "received": list(reversed(rows))[:50]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/api/v5/ai-agent/transcripts":
            return self._send(404, {"error": "not found",
                                    "hint": "POST /api/v5/ai-agent/transcripts"})
        if not self._authed():
            return self._send(401, {"error": "unauthorized",
                                    "hint": "send X-API-Key"})

        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BYTES:
            return self._send(413, {"error": "payload too large",
                                    "limit_bytes": MAX_BYTES, "received_bytes": n})
        raw = self.rfile.read(n)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as ex:
            return self._send(400, {"error": "invalid JSON", "detail": str(ex)})

        errs = validate(payload)
        if errs:
            self.log_message("REJECTED %s (%d problem(s))", payload.get("session_id", "?"), len(errs))
            return self._send(400, {"error": "validation failed", "problems": errs})

        sid = payload["session_id"]
        replaced = sid in STATE["seen"]
        receipt = "rcpt_" + uuid.uuid4().hex[:16]
        STATE["seen"][sid] = receipt

        rec = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "receipt_id": receipt,
            "session_id": sid,
            "channel": payload.get("channel", "chat"),
            "agent": payload.get("agent"),
            "turns": len(payload["turns"]),
            "declared_outcome": payload.get("outcome"),
            "excluded": bool(payload.get("exclude")),
            "has_platform_signals": "platform_signals" in payload,
            "contract_version": payload.get("contract_version"),
            "conversation_id": payload.get("conversation_id", sid),
            "bytes": n,
            "replaced_previous": replaced,
        }
        with STATE["log"].open("a") as f:
            f.write(json.dumps(rec) + "\n")

        if payload.get("exclude"):
            self.log_message("EXCLUDED %s — counted and discarded", sid)
            return self._send(202, {"receipt_id": receipt, "status": "excluded",
                                    "note": "counted and discarded, nothing stored or analysed"})

        self.log_message("ACCEPTED %s  %s  %d turns%s",
                         sid, payload.get("channel", "chat"), len(payload["turns"]),
                         "  (replaces earlier submission)" if replaced else "")
        return self._send(202, {"receipt_id": receipt, "status": "accepted",
                                "replaced_previous": replaced})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--key", default=STATE["key"])
    ap.add_argument("--log", default="received.jsonl")
    a = ap.parse_args()
    STATE["key"] = a.key
    STATE["log"] = Path(a.log)
    print(f"mock ingest on http://0.0.0.0:{a.port}")
    print(f"  POST /api/v5/ai-agent/transcripts   X-API-Key: {a.key}")
    print(f"  GET  /received                      GET /health")
    print(f"  log: {a.log}\n")
    HTTPServer(("0.0.0.0", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
