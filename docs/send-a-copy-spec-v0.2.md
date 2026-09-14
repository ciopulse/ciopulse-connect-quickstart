# ciopulse · Send-a-Copy Spec v0.2

**The whole integration on one page.** When a conversation with your AI agent ends, POST a copy of the transcript to one ciopulse endpoint. That's it — ciopulse handles analysis, redaction, and reporting on its side. If ciopulse is ever unreachable, your agent doesn't notice; you just miss that data point.

**Status:** Draft for review · 14 Sep 2026 · Contact: ciopulse
**Changes from v0.1:** `channel` accepts `"voice"` (text turns from a speech-to-text transcript); an optional `platform_signals` block carries measurements your conversation platform already computed. Everything in v0.1 is unchanged and a v0.1 payload is still accepted.

---

## Endpoint

```
POST https://app.cio-pulse.com/api/v5/ai-agent/transcripts
Content-Type: application/json
```

**Auth** (either): HTTP Basic — username = portal code, password = API key · or `X-API-Key` header. Both supplied by ciopulse at onboarding. HTTPS only.

## Payload

| Field | Required | Description |
|-------|:---:|-------------|
| `contract_version` | ✔ | `"0.2"` (`"0.1"` still accepted for chat) |
| `session_id` | ✔ | Unique ID for this conversation, 1–50 chars. **Must be the same value passed as `tid` on the survey URL** — it's how survey and transcript join. For Amazon Connect this is the `ContactId` |
| `agent.id` | ✔ | Stable identifier of the AI agent |
| `agent.version` | ✔ | Release/version string (scorecards are per-version) |
| `started_at`, `ended_at` | ✔ | ISO 8601 with timezone |
| `turns[]` | ✔ | Ordered, chronological. Each: `role` (`"user"` \| `"agent"` \| `"system"`), `text`, `ts` (ISO 8601 with timezone) |
| `channel` | – | `"chat"` (default) or **`"voice"`** (new in v0.2). Voice means the turns are text produced by speech-to-text; ciopulse reads them exactly as chat. Voice requires `contract_version` `"0.2"`. Audio is never sent |
| `outcome` | – | Your call: `"contained"` \| `"escalated"` \| `"abandoned"` \| `"unknown"` |
| `events[]` | – | e.g. `{"type": "escalation_to_human", "ts": "…"}` |
| `platform_signals` | – | **New in v0.2.** Measurements your platform already computed about this conversation. See below. Omit the block entirely if you have none |
| `metadata` | – | Free-form object, ≤ 2 KB (queue name, deployment context, …) |
| `exclude` | – | `true` = ciopulse must not store or analyse this conversation. It is counted and discarded. A sender MAY send a single placeholder turn instead of the transcript when `exclude` is `true` |

### `platform_signals` (optional, v0.2)

Objective measurements from the platform that ran the conversation (for example a contact-centre platform's own analytics). **ciopulse displays these as comparators next to its own reading of the transcript. They are never inputs to ciopulse scoring.** Field names are deliberately platform-neutral. Populate only what you have; every field is optional.

| Field | Type | Meaning |
|---|---|---|
| `overall_sentiment_user` | number | The platform's overall sentiment for the user, on the platform's own scale (Connect: −5 to +5) |
| `sentiment_by_period[]` | array of `{ "period": n, "score": number }` | The user's sentiment across equal periods of the conversation, in order (Connect: four quarters) |
| `sentiment_shift_user` | `{ "begin": number, "end": number }` | The user's sentiment at the start and end, where the platform reports that instead of periods |
| `talk_time_ms` | integer | Total talk time, voice only |
| `non_talk_time_ms` | integer | Total silence and hold time, voice only |
| `interruptions` | integer | Count of interruptions, voice only |
| `response_time_ms` | integer | Average agent response time, chat only |
| `summary` | string, ≤ 2,000 chars | The platform's generated post-conversation summary, if any. Must be from the redacted conversation |

Unknown fields inside `platform_signals` are ignored, not rejected.

## Example (voice)

```json
{
  "contract_version": "0.2",
  "session_id": "3f1c9a2e-5b7d-4e8f-9a0b-1c2d3e4f5a6b",
  "agent": { "id": "service-desk-agent", "version": "1.4.2" },
  "channel": "voice",
  "started_at": "2026-09-02T00:14:00.000+00:00",
  "ended_at": "2026-09-02T00:14:47.000+00:00",
  "outcome": "unknown",
  "turns": [
    { "role": "agent", "text": "Hi, you've reached the IT service desk. How can I help today?", "ts": "2026-09-02T00:14:00.000+00:00" },
    { "role": "user",  "text": "Yeah hi um I can't get onto the VPN it keeps saying authentication failed and I've tried like three times already", "ts": "2026-09-02T00:14:03.800+00:00" },
    { "role": "agent", "text": "Sorry about that. Can I confirm your staff email so I can check the account?", "ts": "2026-09-02T00:14:12.600+00:00" },
    { "role": "user",  "text": "it's [PII]", "ts": "2026-09-02T00:14:17.000+00:00" },
    { "role": "agent", "text": "Thanks. Your VPN certificate expired yesterday. I've issued a new one; it will install in about two minutes. Then disconnect and reconnect.", "ts": "2026-09-02T00:14:19.500+00:00" },
    { "role": "user",  "text": "no worries that's sorted thanks", "ts": "2026-09-02T00:14:41.000+00:00" }
  ],
  "platform_signals": {
    "overall_sentiment_user": -0.5,
    "sentiment_by_period": [
      { "period": 1, "score": -2.5 }, { "period": 2, "score": -1.0 },
      { "period": 3, "score": 0.0 },  { "period": 4, "score": 2.5 }
    ],
    "talk_time_ms": 31000,
    "non_talk_time_ms": 9000,
    "interruptions": 1,
    "summary": "Customer could not connect to the VPN because their certificate had expired. The agent issued a new certificate and told the customer to reconnect after two minutes."
  },
  "metadata": { "source": "amazon-connect", "queue": "General Enquiries" }
}
```

## Behaviour

- **Response:** `202 Accepted` + a receipt ID. Validation is synchronous (auth, schema, size); everything else is asynchronous. Typical response < 500 ms.
- **Duplicates / retries:** re-POSTing the same `session_id` within 30 days replaces the earlier submission — retrying is always safe.
- **Limits:** payload ≤ 1 MB · ≤ 500 turns · `metadata` ≤ 2 KB · per-key rate limit. Errors: `400` (field-level detail), `401`, `413`, `429` (with `Retry-After`).
- **When to send:** once, when the conversation ends. Retry on `5xx` with backoff if convenient — or don't; a missed transcript is acceptable by design.
- **Voice transcripts** read differently from chat (disfluencies, transcription errors). Send them as they are; do not clean them up. ciopulse's reader accounts for speech-to-text artefacts.

## What happens on the ciopulse side (so your security review is short)

- Transcripts are **PII-redacted in-memory on arrival, before anything is persisted** — raw text is never written to disk, queue, or logs.
- You control what's sent: pre-redact if you wish, skip fields, or set `exclude: true` for sensitive conversations. Platforms that can redact before export (Amazon Connect's `RedactedOnly` option, for example) mean unredacted text never leaves your account at all; ciopulse then redacts a second time as defence in depth.
- `platform_signals` are stored alongside the transcript and shown as comparators. They do not influence ciopulse scoring.
- All processing and storage in **AWS ap-southeast-2 (Sydney)**. ciopulse never sits in your traffic path and holds no credentials to your systems.

## Reference implementation

For Amazon Connect, the open-source **ciopulse Connect quickstart** (github.com/ciopulse/ciopulse-connect-quickstart) is a SAM package that installs in your own AWS account and implements this contract end to end from Connect's redacted analytics output.

---

*Spec version 0.2 — draft. Field additions will be backwards-compatible within v0.x; breaking changes bump the version. Questions → ciopulse.*
