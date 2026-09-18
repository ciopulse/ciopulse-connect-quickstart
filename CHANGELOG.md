# Changelog

All notable changes to this project are recorded here. The format follows Keep a Changelog; versions follow semantic versioning.

## [0.2.0] - 2026-09-18

Sends send-a-copy contract **v0.3**. Every valid v0.2 payload is also valid v0.3, so a receiver that accepts 0.3 needs no other change to keep working.

### Added
- `conversation_id` on every payload: Connect's `InitialContactId`, equal to `session_id` unless an agent transferred the contact. This is the value the survey `tid` carries; the survey guide already builds it from `$.InitialContactId`.
- `turns[].actor` on every `agent` turn: `bot` for Connect roles SYSTEM, BOT and CUSTOM_BOT; `human` for AGENT and SUPERVISOR. Omitted when the role is unknown and on non-agent turns.
- Excluded stubs carry `conversation_id` too.
- Mock receiver accepts 0.3, validates `conversation_id` (1–50 chars) and `actor` (`bot` or `human`, agent turns only), and logs `has_platform_signals` instead of the old `has_signals`.

### Changed
- `contract_version` is now `"0.3"`.

## [0.1.0] - never released

Internal preview, superseded by 0.2.0 before any external install. Verified end to end on a live Amazon Connect instance in ap-southeast-2.

### Added
- SAM template: Lambda forwarder, scoped IAM role, two EventBridge rules on S3 object-created events, log group, failed-delivery alarm.
- Voice and chat parsers for Contact Lens redacted analysis files, with sanitised real-shape fixtures.
- Delivery to the ciopulse send-a-copy endpoint, contract v0.2, with retries on 5xx/429/network and idempotent re-delivery.
- `platform_signals` enrichment from Connect's own analytics, comparators only.
- Queue exclusion (`exclude: true` stubs), outcome mapping from a contact attribute, `escalation_to_human` events.
- Transfer linkage: `initial_contact_id` and `previous_contact_id` in metadata for transferred legs.
- `tools/mock-receiver/`: the strict contract validator, runnable locally or as a Lambda function URL for in-account tests.
- `tools/sandbox-bot/`: a generative Lex V2 bot backed by Amazon Bedrock for testing bot-to-human contacts.
- `tools/sandbox_chat.py` and `tools/create-sandbox-instance.sh` for a throwaway test instance.

### Verified on a live instance
- Chat analysis lands at the bucket root under `Analysis/Chat/Redacted/`; the default key patterns match it.
- Flow and bot messages carry participant role `SYSTEM`; a human agent is `AGENT`.
- Voice millisecond offsets count from agent connection; timestamps are anchored accordingly.
- A flow-level transfer to queue keeps one contact ID; an agent-initiated transfer mints a new one linked by `InitialContactId`.

### Known limitations
- Voice contacts with a bot leg before a human have not been tested.
- The transferred leg of an agent-initiated transfer produced no analysis file in testing; the stock queue-transfer flow lacks the analytics block. Add it to that flow.
- How Amazon's native Connect AI agent is labelled in transcripts, as opposed to a Lex bot, is unknown.
- Kinesis real-time delivery is documented as a stub, not built.
