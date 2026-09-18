# RUN-LOG — build notes and things to verify against a live instance

**Built:** 14 September 2026 · **Tooling used:** Python 3.14 venv with boto3 1.43, pytest 8, cfn-lint 1.56 · **Not used:** AWS SAM CLI (not installed on the build machine; see item 1)

Everything below is either a decision that deserves a second look or a place where the AWS documentation did not settle the question. Each item says what the code does today and what a five-minute check on a live Connect instance would confirm.

## What ran

| Step | Result |
|---|---|
| `pytest -q` (parsers, handler end-to-end against the vendored mock receiver, hygiene) | **40 passed** (12 parser, 25 handler and delivery, 3 hygiene) |
| `cfn-lint template.yaml` | **clean**, no warnings |
| `sam build` / `sam local invoke` | **not run**, SAM CLI not installed. The end-to-end path was exercised by invoking `lambda_handler` directly with the EventBridge and classic-S3 fixtures, with S3, Secrets Manager and Connect stubbed and the real mock receiver on a socket. The `sam local invoke` command in the README is documented, not executed |
| Deployment | none, per the brief |
| Client-name and credential grep | **clean** on the whole tree; also enforced on every run by `tests/test_repo_hygiene.py` (the forbidden list is base64-encoded so it is not itself a hit) |

## Live-instance verification, 15 September 2026

A sandbox instance (`ciopulse-sandbox`, ap-southeast-2) was created with `tools/create-sandbox-instance.sh`, a bot-only inbound flow with analytics and RedactedOnly was added, and one chat was driven through the API. Findings, each of which closed or narrowed an item below:

| Fact | Verified |
|---|---|
| Chat analysis lands at the **bucket root**: `Analysis/Chat/Redacted/YYYY/MM/DD/<contactId>_analysis_redacted_<ts>.json`, not under the chat-transcript storage prefix | ✅ item 2 closed for chat |
| Raw chat transcript lands under the storage prefix: `connect/<alias>/ChatTranscripts/YYYY/MM/DD/<contactId>_<ts>.json` | ✅ |
| Flow and bot messages carry `ParticipantRole: SYSTEM`; the `Participants` array lists CUSTOMER and SYSTEM | ✅ item 3 closed; default agent roles now include SYSTEM |
| Analysis file appeared ~4 minutes after the chat ended | ✅ |
| `ConversationCharacteristics` shape: sentiment, shift and response time nest under `DetailsByParticipantRole`; chat has `DetailsByTranscriptItemGroup[].ProgressiveScore` instead of `SentimentByPeriod`; no `TotalConversationDurationMillis`; `ContactSummary.SummaryItemsDetected[]` with no generated summary text; `ResponseTime.AutomatedInteractionGreetingTimeMillis` for a bot | ✅ item 6 closed for chat; parser updated, sanitised file is now `fixtures/contact-lens-chat-redacted.json` |
| Redaction worked: an email address in the customer message was replaced and `Redaction.CharacterOffsets` recorded it | ✅ |
| `redaction_option: RedactedOnly` set through the flow block's `AnalyticsRedactionResults` produced only the redacted file | ✅ |

| Forwarder deployed with `sam deploy` (stack tags required by the account's SCP), pointed at the in-account mock receiver (`tools/mock-receiver/`). A second chat ended at 05:11:16Z; the analysis file landed, the EventBridge rule matched the root-level key with the default `*Analysis/Chat/Redacted/*.json` wildcard, and the forwarder posted within ~3 minutes of the chat ending: `202`, 5 turns, `platform_signals` present, 281 ms in the Lambda | ✅ item 1 closed for chat; end-to-end proven |

**Voice, verified 16 September 2026** with a claimed Sydney DID, a flow that transfers to BasicQueue, and a human answering in the softphone:

| Fact | Verified |
|---|---|
| Voice analysis lands at the bucket root too: `Analysis/Voice/Redacted/YYYY/MM/DD/<contactId>_analysis_redacted_<ts>.json`, with the redacted `.wav` beside it. The raw recording stays under the storage prefix `connect/<alias>/CallRecordings/…` | ✅ item 2 closed for voice |
| The `.wav` never triggered the forwarder (wildcard ends in `.json`) | ✅ |
| File shape matches the documented one: `Version 1.1.0`, `Participants` with `ParticipantId` = `ParticipantRole` (`CUSTOMER`, `AGENT`), segments with millisecond offsets and no `ParticipantRole` key, `Sentiment.OverallSentiment` flat per participant, `SentimentByPeriod.QUARTER`, `TalkTime`/`NonTalkTime`/`Interruptions`/`TalkSpeed` with `DetailsByParticipant`, `TotalConversationDurationMillis`, an extra `CustomModels` key, and **no `ContactSummary`** (generated summaries not enabled) | ✅ item 6 closed for voice; sanitised file is now `fixtures/contact-lens-voice-redacted.json` |
| **Offsets count from the agent-connect moment**: the `<ts>` in the filename equals `AgentInfo.ConnectedToAgentTimestamp` and the first segment sits 18 s into the recording. Anchoring on `InitiationTimestamp` put turns ~15 s early | ✅ item 4 closed; handler now anchors on `ConnectedToAgentTimestamp` when present and records `metadata.offset_anchor` |
| `AgentInfo` present → `escalation_to_human` emitted, even though no bot preceded the human | ✅ item 5 narrowed: semantics documented in the README; a bot-then-transfer contact is still untested |
| Analysis file appeared ~5 minutes after hang-up; the forwarder delivered 5 turns with signals, `202`, 2.7 s | ✅ end-to-end for voice |

**Generative bot, verified 16 September 2026** with `tools/sandbox-bot/` (Lex V2 + Bedrock code hook) in the flow:

| Fact | Verified |
|---|---|
| A Lex V2 bot's messages arrive in the chat analysis file as `ParticipantRole: SYSTEM`, exactly like flow messages; `Participants` lists CUSTOMER and SYSTEM only. Bot-only chats (no human) are analysed and produce customer sentiment | ✅ item 3 closed for chat: SYSTEM is the right default agent role for Connect-native bots |
| Connect defaults a chat with no language to `en-US`; a bot without an `en_US` locale makes the Lex block fail with no error message (flow log shows `GetUserInput … Results: Error`) | ✅ documented in `docs/sandbox-instance.md` |
| Bot-flagged escalation (`$.Lex.SessionAttributes.escalate`) routed the chat to the queue; with no agent staffed it waited in queue | ✅ flow branch works |

**Bot → human → agent transfer, verified 16 September 2026** (chat; a human answered the bot's escalation, then transferred the chat from the CCP via a queue quick connect):

| Fact | Verified |
|---|---|
| The bot's escalation *within the flow* (transfer-to-queue block) keeps the **same** `ContactId`; the human's leg is appended to the same contact and the analysis file has three participants: CUSTOMER, SYSTEM (bot), AGENT (human) | ✅ one contact, one file, 13 turns forwarded under one `session_id` |
| An **agent-initiated transfer** (CCP quick connect) mints a **new `ContactId`** with `InitiationMethod: TRANSFER`, `InitialContactId` and `PreviousContactId` pointing at the first contact; the first contact is disconnected at that moment. Each leg gets its own raw transcript file; the second leg's raw transcript carries `InitialContactId` too | ✅ |
| The forwarder therefore sends one payload per leg, `session_id` = that leg's `ContactId`. It now adds `metadata.initial_contact_id` / `previous_contact_id` on transferred legs so the receiver can stitch them | ✅ code change in this commit |
| **Design consequence for the survey:** the Disconnect flow runs on the *last* leg. Build the survey `tid` from `$.InitialContactId` (falls back to the contact's own id when there was no transfer) so one conversation yields one survey, joined to the first leg where the AI agent was | recorded here and in `docs/survey-flow.md` |
| **No analysis file appeared for the transferred leg** (12 minutes; the first leg's arrived in 4.5). Most likely cause: a transferred contact runs the *queue transfer* flow, and the stock "Default queue transfer" flow has no *Set recording and analytics behavior* block, so analytics was never enabled on leg two. A second possible cause is that the customer sent no message in that leg. **Customer guidance either way: put the analytics block in the transfer flow as well as the inbound flow**, or the human leg of an escalated conversation is never analysed | ⚠️ recorded; retest with a patched transfer flow pending |

**Contract v0.3, verified 18 September 2026.** The 0.2.0 forwarder and mock were redeployed to the sandbox and three saved real contacts were replayed. The mock accepted all three as `contract_version` 0.3:

| Contact | Channel | Turns | `actor` values seen | `conversation_id` |
|---|---|---|---|---|
| Bot, then human in the same contact | chat | 13 | bot, human | equals `session_id` (no agent transfer) |
| Human-answered call | voice | 5 | human | equals `session_id` |
| Bot only | chat | 5 | bot | equals `session_id` |

The transferred leg of the earlier agent transfer has no analysis file (see above), so `conversation_id` differing from `session_id` is covered by unit tests only.

Still unverified: the same for voice with a bot leg before the human, and how a Connect-native AI agent (Amazon Q in Connect) is labelled, as opposed to a Lex bot.

## 1. Trigger: S3 events through EventBridge, not S3-to-Lambda notifications

**What the code does.** Two EventBridge rules match `aws.s3` / `Object Created` events on the bucket with a `wildcard` key filter. This is the reliable `aws.s3` source, not the CloudTrail-backed `aws.contact-lens` source the dev notes warn against.

**Why.** CloudFormation cannot attach a notification to a bucket it does not own, and `PutBucketNotificationConfiguration` replaces the whole configuration, which is dangerous on a bucket Connect already uses. EventBridge rules attach to any bucket without touching it. The cost is one manual step: enabling "Amazon EventBridge" on the bucket (console toggle preserves existing notifications; the CLI does not).

**Verify.** After the toggle, one test call should produce a `Forwarded` metric within a few minutes. If not, check the rule's *Monitoring* tab for matched events.

## 2. Chat key prefix and file name (AWS does not document them)

**What the code does.** `ChatKeyPattern` defaults to `*Analysis/Chat/Redacted/*.json`. The same string drives the EventBridge wildcard, the IAM resource and the handler's own `fnmatch` check. Contact ID is taken from the file name (`<uuid>_…`) with a fallback to `CustomerMetadata.ContactId` inside the file.

**Verified 15 Sep 2026 (chat):** the file is at the bucket root, `Analysis/Chat/Redacted/YYYY/MM/DD/<contactId>_analysis_redacted_<ts>.json`. The default pattern matches it because `*` matches the empty prefix. Voice still to confirm.

Also unverified: whether the voice file name is exactly `<contactId>_analysis_redacted_<ts>.json`. The tests use a stricter pattern for the negative case; the default pattern is deliberately loose.

## 3. Participant roles for bots and AI agents

**What the code does.** `CUSTOMER` → `user`; anything in `AgentParticipantRoles` (default `AGENT,BOT,CUSTOM_BOT`) → `agent`; everything else → `system`. Role is taken from the item's `ParticipantRole`, then the `Participants` array, then the `ParticipantId` itself (voice files use `AGENT`/`CUSTOMER` as ids).

**The open question.** How a Connect AI agent (Q in Connect self-service) or a Lex bot appears in the Contact Lens transcript: as `AGENT`, `SYSTEM`, `CUSTOM_BOT`, or something else. AWS's May 2026 self-service evaluation feature implies bot turns are in the transcript, but the role label is not documented.

**Verified 15 Sep 2026:** flow (`MessageParticipant`) messages are `SYSTEM`. Default `AgentParticipantRoles` now includes `SYSTEM`; joins and leaves are `Type: EVENT` and filtered regardless. A Lex bot or Connect AI agent has not been tested and may use a different role.

## 4. Voice timestamps: what the millisecond offsets are relative to

**What the code does.** `started_at` = `DescribeContact.InitiationTimestamp`; turn `ts` = `started_at + BeginOffsetMillis`; `ended_at` = `DisconnectTimestamp`. If `DescribeContact` fails, `started_at` = object `LastModified` − `TotalConversationDurationMillis` and `metadata.timestamps_estimated` = `true`.

**The imprecision.** Offsets are relative to the start of the analysed audio, which begins when analytics starts in the flow, not at contact initiation. For a contact with IVR time before the agent, turn timestamps will be early by that amount. Turn *order* is unaffected, and the survey join uses `session_id`, not time.

**Verified 16 Sep 2026:** offsets are relative to `AgentInfo.ConnectedToAgentTimestamp`. The handler now anchors there when the field exists and falls back to `InitiationTimestamp` otherwise, recording which in `metadata.offset_anchor`.

## 5. Escalation detection

**What the code does.** An `escalation_to_human` event is emitted when `DescribeContact` returns `AgentInfo.ConnectedToAgentTimestamp`, or (fallback) when a second distinct agent-role participant first speaks in the transcript.

**The open question.** For a voice contact that starts with an AI agent and is transferred to a person, Contact Lens's two-participant limit means the analysis may cover only one leg. Whether the redacted file for such a contact contains both legs, or two files are written, is not documented.

**Verify.** Run one escalated test call and see how many analysis files appear and what `Participants` contains in each.

## 6. `ConversationCharacteristics` field shapes

Defensive parsing was used wherever AWS has published more than one nesting:

- **Summary:** both `ContactSummary.AutoGenerated.PostContactSummary.Content` (current) and `ContactSummary.PostContactSummary.Content` (an older documented shape) are accepted.
- **Chat overall sentiment:** `OverallSentiment.CUSTOMER` first, then `OverallSentiment.DetailsByInteraction.WithAgent.CUSTOMER`.
- **Chat response time:** `ResponseTime.DetailsByParticipant.AGENT.AverageMillis` (also `AverageResponseTimeMillis`, `Average`), falling back to `AgentGreetingTimeMillis`. **The exact key names are a guess** from the dev notes' description; the fixture uses `AverageMillis`.
- **Sentiment values** may be numbers or `{Score: n}` objects; both are read.

**Verify.** Diff one real redacted file against `fixtures/contact-lens-voice-redacted.json` and `fixtures/contact-lens-chat-redacted.json`. Any key that differs is a one-line change in `src/parsers/`.

## 7. Name of the enrichment block

The enrichment block is `platform_signals`, with platform-neutral scalar names: `overall_sentiment_user`, `sentiment_by_period[]`, `sentiment_shift_user`, `talk_time_ms`, `non_talk_time_ms`, `interruptions` (a count), `response_time_ms` and `summary`. An earlier internal draft used `signals` with different fields; contract v0.3 settles on `platform_signals`, and the bundled mock receiver logs `has_platform_signals`.

## 8. Excluded contacts are sent as a stub

For a queue in `ExcludeQueueNames`, the forwarder never downloads the transcript. It sends the required envelope, `exclude: true`, and one placeholder `system` turn (`"excluded by sender"`). This satisfies the contract's non-empty `turns` rule while keeping transcript text inside the account. The v0.2 spec records this as permitted sender behaviour. If ciopulse would rather receive nothing at all for excluded queues, remove the `_deliver` call in the exclusion branch and count locally instead.

## 9. Over-cap contacts are dropped, not chunked

Spec limit is 500 turns / 1 MB. Consecutive same-speaker voice segments are merged first (a spoken turn often arrives as several segments). Anything still over the cap is logged with reason `too_many_turns` or `payload_too_large`, counted as `Failed`, and dropped. Chunking was rejected because `session_id` is the join key and a re-POST replaces the earlier submission, so chunks would overwrite each other.

## 10. Contract version is always 0.2

Both channels send `contract_version: "0.2"`. Chat would be valid on 0.1, but `platform_signals` is a 0.2 feature and one version is simpler to support. Additions in v0.x are backwards-compatible.

## 11. Secret format

The Secrets Manager secret may be a plain string or JSON with `api_key` (also `apiKey`, `key`, or the first value). Cached in memory for the life of the Lambda container; rotating the key needs a redeploy or a wait for container recycling. Acceptable for a quickstart; note it if key rotation is part of the customer's policy.

## 12. Licence

MIT, copyright ciopulse. MIT was chosen as the least-friction option for a customer-installed package. Change `LICENSE` and the README footer if Apache-2.0 (patent grant) is preferred.

## 13. Metrics via embedded metric format

Metrics are emitted as EMF JSON on stdout rather than `PutMetricData`, so the role needs no CloudWatch permission and there is no extra API call per contact. The one dimension is `Stack`. The alarm in the template keys on it.

## 14. Things deliberately not built

Kinesis real-time path (`src/kinesis_stub.py` documents where it attaches), audio of any kind, backfill of historical contacts (no documented API retrieves a completed analysis by contact ID; a one-off S3 listing script would do it), and a DLQ on the EventBridge rules (retry policy of 3 attempts over 1 hour is configured; add a DLQ if a customer wants durable failure capture).
