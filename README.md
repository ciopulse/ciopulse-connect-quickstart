# ciopulse Connect quickstart

Forward redacted Amazon Connect conversation transcripts to ciopulse, from your own AWS account, in about fifteen minutes.

This package is a small AWS Lambda function plus a SAM template. It watches the redacted output of Amazon Connect's conversational analytics in your S3 bucket, maps each finished contact to the ciopulse *send-a-copy* payload, and POSTs it to ciopulse. Nothing else is created in your account, no audio is ever read, and ciopulse never holds credentials to your systems.

> **Status:** v0.1.0 · targets send-a-copy contract v0.2 · Python 3.12, boto3 only · MIT licence

---

## Contents

1. [What this is](#1-what-this-is)
2. [Prerequisites](#2-prerequisites)
3. [Deploy in 15 minutes](#3-deploy-in-15-minutes)
4. [What gets sent, and what never leaves your account](#4-what-gets-sent-and-what-never-leaves-your-account)
5. [Post-conversation survey (SMS and chat)](#5-post-conversation-survey-sms-and-chat)
6. [Metrics, alarms and troubleshooting](#6-metrics-alarms-and-troubleshooting)
7. [Testing locally](#7-testing-locally)
8. [Contract reference](#8-contract-reference)
9. [Design notes](#9-design-notes)

---

## 1. What this is

ciopulse measures the experience people have with your AI service agent by combining two lenses: what the person **says** in a short post-conversation survey, and what the transcript **shows**. This quickstart supplies the second lens. It runs inside your account, reads only the transcripts Connect has already redacted, and sends them to one ciopulse endpoint.

The survey lens is a Connect flow change, not code. Section 5 walks through it.

## 2. Prerequisites

| Requirement | Why | Where |
|---|---|---|
| **Conversational analytics enabled on the contact flow** | Transcripts only exist when the `Set recording and analytics behavior` block has *analytics* on. Recording alone produces audio, not text. | Flow designer, and the instance-level analytics setting |
| **`redaction_option` = `RedactedOnly`** (recommended) or `RedactedAndOriginal` | The forwarder reads the `…/Redacted/` prefix and nothing else. With `RedactedOnly`, an unredacted transcript is never written anywhere. | `Set contact attributes` block, or a Lambda in the flow, before analytics starts |
| **The analytics S3 bucket name** | The event rules and the IAM policy are scoped to it. | Connect console → your instance → Data storage → *Chat transcripts* / *Call recordings* |
| **A ciopulse API key in Secrets Manager** | The Lambda reads the key at runtime. The template takes the secret's **ARN**, never the key. | `aws secretsmanager create-secret --name ciopulse/api-key --secret-string '<key>'` |
| **AWS SAM CLI** and permissions to create a Lambda, IAM role, EventBridge rules and an alarm | Deployment | `brew install aws-sam-cli` or [AWS docs](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) |
| An SMS-capable phone number claimed to the instance | Only if you use the post-call SMS survey (section 5) | Connect console → Channels → Phone numbers |

The forwarder does **not** need Kinesis, EventBridge Contact Lens events, the analytics data lake, or any real-time API.

## 3. Deploy in 15 minutes

```bash
git clone https://github.com/ciopulse/ciopulse-connect-quickstart.git
cd ciopulse-connect-quickstart
sam build
sam deploy --guided
```

The guided deploy asks for these values. Five have no default and you must supply them:

| Parameter | Required | What to enter |
|---|:---:|---|
| `TranscriptBucketName` | ✔ | The analytics bucket, for example `amazon-connect-abc123` |
| `ConnectInstanceArn` | ✔ | `arn:aws:connect:<region>:<account>:instance/<id>`. Used for read-only contact metadata (timestamps, queue, attributes) |
| `ApiKeySecretArn` | ✔ | ARN of the Secrets Manager secret holding your ciopulse API key |
| `AgentId` | ✔ | A stable name for your AI agent, for example `service-desk-agent`. This is how it appears in ciopulse |
| `AgentVersion` | ✔ | Your release string, for example `1.4.2`. **Bump it on each release**: ciopulse scorecards are per version |
| `CiopulseEndpoint` | | Defaults to the production endpoint. Point it at a test receiver during setup |
| `VoiceKeyPattern` / `ChatKeyPattern` | | Default to `*Analysis/Voice/Redacted/*.json` and `*Analysis/Chat/Redacted/*.json`. See section 9 before changing |
| `ExcludeQueueNames` | | Comma-separated queue names whose contacts are sent with `exclude: true` (counted by ciopulse, never stored) |
| `OutcomeAttributeName` | | A contact attribute your flow sets to `contained`, `escalated` or `abandoned`. Leave blank to send `unknown` |
| `SkipMultiPartyContacts` | | `true` to skip contacts with more than two participants. Default `false` |
| `AgentParticipantRoles` | | Participant roles mapped to the AI agent. Default `AGENT,BOT,CUSTOM_BOT,SYSTEM` (flow and bot messages carry `SYSTEM`) |
| `AlarmSnsTopicArn` | | Optional SNS topic for the failed-delivery alarm |

**One manual step after the stack is up.** The forwarder listens for S3 "Object Created" events through EventBridge, and an existing bucket does not emit those until you switch them on. In the S3 console open the bucket → *Properties* → *Amazon EventBridge* → **On**. Or, if the bucket has **no other** event notifications configured:

```bash
aws s3api put-bucket-notification-configuration \
  --bucket <TranscriptBucketName> \
  --notification-configuration '{"EventBridgeConfiguration": {}}'
```

That CLI call replaces the bucket's whole notification configuration, so use the console toggle if other notifications already exist. The stack's `EnableEventBridgeCommand` output repeats the command with your bucket name filled in.

Then make one test call or chat and wait for analytics to finish. On a live chat the transcript reached the receiver about three minutes after the chat ended, and check the `Forwarded` metric or the Lambda log group. A `202` in the log means ciopulse accepted the transcript.

## 4. What gets sent, and what never leaves your account

**Sent, per contact, as one JSON document over HTTPS:**

- `session_id`: the Connect `ContactId`. This is also the `tid` on the survey link, which is how survey and transcript join.
- `agent.id`, `agent.version`: the two template parameters.
- `channel`: `voice` or `chat`, from the S3 path.
- `started_at`, `ended_at`: from the contact record.
- `turns[]`: the redacted transcript text with a role (`user`, `agent` or `system`) and a timestamp per turn. Messages your flow or bot sends arrive from Connect with role `SYSTEM` and are mapped to `agent` by default. Consecutive voice segments from the same speaker are merged into one turn.
- `outcome`: `unknown`, unless you set `OutcomeAttributeName`.
- `events[]`: an `escalation_to_human` event when a human agent was connected, or when a second agent-role participant appears in the transcript.
- `platform_signals` (optional): numbers Connect already computed, forwarded as-is. Customer sentiment overall and by period, talk and non-talk time, interruption count, agent response time, and the generated contact summary. **ciopulse displays these as comparators next to its own reading. They are never used as scoring inputs.** The block is omitted entirely when analytics did not produce it.
- `metadata`: queue name, initiation method, forwarder version. Under 2 KB.

**Never sent, never read:**

- Audio. The IAM policy allows `s3:GetObject` on `*.json` under the redacted prefixes only. The `.wav` files next to them are not readable by this role.
- Unredacted transcripts. The role has no access to the unredacted prefixes, and the handler refuses any key without `/Redacted/` in its path even if an event for one arrives.
- Phone numbers, customer names, display names, attachment names. Connect redacts these before the file is written.
- Contact attributes other than the one you name in `OutcomeAttributeName`.

**Never logged:** transcript text, summaries, or any payload field. Logs carry the `ContactId`, sizes, HTTP status codes, attempt counts and timings only.

**Excluded queues** are sent as a stub: the required envelope, `exclude: true`, and a single placeholder turn. ciopulse counts the contact and discards it; no transcript text leaves your account for those.

**Retries are safe.** ciopulse replaces an earlier submission with the same `session_id` within 30 days, so a retried or duplicate delivery never double-counts.

## 5. Post-conversation survey (SMS and chat)

The survey is delivered by Connect, not by ciopulse, so ciopulse never sees a phone number. The full step-by-step is in [docs/survey-flow.md](docs/survey-flow.md). In short:

1. In the **Disconnect flow**, add `Set contact attributes` → user-defined attribute `survey_url` = your ciopulse survey link with `tid=$.ContactId`.
2. Add `Send message` (SMS) with a body under 1,024 characters that references `$.Attributes.survey_url`.
3. Optionally put a `Distribute by percentage` block in front to sample. Start at 100%.
4. For chat, put the same link in the bot's goodbye message.

An SMS-capable origination number must be claimed to the instance first. In Australia, carrier registration for that number takes weeks, so start it early.

## 6. Metrics, alarms and troubleshooting

Metrics are published to CloudWatch namespace `ciopulse/ConnectForwarder` with one dimension, `Stack` = your stack name. They are emitted through the log stream (embedded metric format), so no extra IAM permission is involved.

| Metric | Meaning |
|---|---|
| `Forwarded` | ciopulse returned `202` for a transcript |
| `Excluded` | Sent with `exclude: true` (queue exclusion); ciopulse returned `202` |
| `Failed` | Delivery gave up: three attempts on `5xx`/`429`/network, or a non-retryable `4xx` |
| `ParseErrors` | The S3 object could not be read as a Connect analytics file |
| `Skipped` | Ignored by design: not a redacted `.json`, or a multi-party contact with `SkipMultiPartyContacts=true` |

The template creates one alarm, `Failed ≥ 1 in 5 minutes`. Pass `AlarmSnsTopicArn` to have it notify you.

Each processed object writes one JSON log line. Look at `status`, `reason` and `http_status`:

| Symptom | Likely cause |
|---|---|
| Nothing happens after a call | EventBridge is not enabled on the bucket (section 3), or analytics is not enabled on the flow, or the object landed outside the key patterns. Check the bucket for `…/Analysis/Voice/Redacted/…` files |
| `status: skipped`, `reason: not_redacted` | Only unredacted files are being written. Set `redaction_option` |
| `status: skipped`, `reason: key_pattern` | Your bucket layout differs from the default patterns. Adjust `VoiceKeyPattern`/`ChatKeyPattern` (section 9) |
| `http_status: 401` | The secret does not hold a valid ciopulse key |
| `http_status: 400` | Contract mismatch. The log line includes ciopulse's field-level problems |
| `http_status: 413` or `reason: too_many_turns` | Contact exceeds 1 MB or 500 turns after merging. Rare; it is logged and dropped |
| `reason: contact_metadata_unavailable` | `DescribeContact` failed. Timestamps are estimated from the file's write time and marked `timestamps_estimated: true` in metadata |

## 7. Testing locally

The repo includes ciopulse's strict mock receiver, `tools/mock-receiver/mock_ingest.py`. A `202` from it means the payload really conforms to the contract; anything else comes back with field-level errors.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                     # parsers, handler, delivery, hygiene
cfn-lint template.yaml
```

To run the mock and post a fixture by hand:

```bash
python3 tools/mock-receiver/mock_ingest.py --key test           # terminal 1, listens on :8088
sam build && sam local invoke ForwarderFunction \
  --event fixtures/events/eventbridge-voice.json \
  --env-vars fixtures/env.local.json               # terminal 2
```

`sam local invoke` still needs real AWS credentials for S3, Secrets Manager and Connect, so for a credential-free run use the unit tests, which stub those clients and drive the handler end to end against the mock.

**In-account end-to-end test.** `tools/mock-receiver/` is the same validator wrapped as a Lambda behind a function URL, so you can prove the whole path inside your account before pointing at ciopulse:

```bash
cd tools/mock-receiver
sam deploy --guided --stack-name ciopulse-mock-receiver     # asks for any MockApiKey string
```

Put the same string in the forwarder's Secrets Manager secret, set `CiopulseEndpoint` to the stack's `Endpoint` output, run a test contact, and watch both log groups: the forwarder logs `Forwarded=1`, the mock logs `accepted` with the turn count. Switch `CiopulseEndpoint` and the secret to production afterwards.

## 8. Contract reference

The payload follows the **send-a-copy contract v0.2**: v0.1 plus `channel: "voice"` and the optional `platform_signals` block. Field additions within v0.x are backwards-compatible.

- Endpoint: `POST https://app.cio-pulse.com/api/v5/ai-agent/transcripts`
- Auth: `X-API-Key` header
- Limits: 1 MB, 500 turns, `metadata` ≤ 2 KB
- Response: `202` with a receipt; `400` with field-level problems; `401`; `413`; `429` with `Retry-After`
- Full text: [docs/send-a-copy-spec-v0.2.md](docs/send-a-copy-spec-v0.2.md)

## 9. Design notes

**Why S3 events via EventBridge, and not "Contact Lens events".** Amazon Connect publishes no "analysis complete" event. The EventBridge entries under the `aws.contact-lens` source are CloudTrail API-call records with best-effort delivery, and Contact Lens rules fire on category match, not completion. S3 object-created events (`aws.s3` source) are the reliable signal that a redacted analysis file exists. Using EventBridge rather than a direct S3-to-Lambda notification lets the stack attach to a bucket it does not own without touching the bucket's existing notification configuration.

**Key patterns.** AWS documents the voice layout (`…/Analysis/Voice/Redacted/YYYY/MM/DD/<contactId>_analysis_redacted_<ts>.json`) but not the chat one. On a live instance (September 2026) chat analysis landed at the **bucket root**, `Analysis/Chat/Redacted/YYYY/MM/DD/<contactId>_analysis_redacted_<ts>.json`, not under the storage prefix configured for chat transcripts. The default patterns `*Analysis/<Channel>/Redacted/*.json` match both the root and a `connect/<alias>/…` prefix, since `*` matches zero or more characters in EventBridge, IAM and the handler alike. The same string drives all three, so changing the parameter changes them together.

**Participant roles.** Voice files label speakers `AGENT` and `CUSTOMER`. Chat files carry per-participant roles: on a live instance, every message sent by the contact flow or a bot arrived as `SYSTEM`, with a real human agent as `AGENT`. `CUSTOMER` maps to `user`; anything in `AgentParticipantRoles` maps to `agent`; everything else becomes `system`. If your flow sends system notices you do not want attributed to the agent, remove `SYSTEM` from the parameter.

**Kinesis.** Connect's documented real-time path is a Kinesis Data Stream of analysis segments. It is not built here; `src/kinesis_stub.py` marks where it would attach. The S3 path is simpler, needs no stream, and post-call latency of a few minutes is fine for a survey-and-transcript join.

**Chunking.** The contract caps a transcript at 500 turns. Since `session_id` is the join key and a re-POST replaces the earlier one, splitting a contact across payloads would overwrite itself. The forwarder therefore merges consecutive same-speaker voice segments and drops anything still over the cap, with a `Failed` metric and a log line.

---

Licence: MIT. Issues and pull requests welcome.
