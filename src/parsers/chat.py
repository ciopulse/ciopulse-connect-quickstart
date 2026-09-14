"""Parser for Amazon Connect chat analytics output (Version CHAT-2022-11-30).

Shape (redacted file): Transcript[] items with absolute timestamps, a Type
(MESSAGE or EVENT) and a ContentType. Only text/plain and text/markdown items
are conversation turns; joins, leaves and attachments are skipped. Character
offsets (used by Connect for highlights) are irrelevant to the contract and
ignored. ConversationCharacteristics adds ResponseTime and SentimentShift.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .common import (DEFAULT_AGENT_ROLES, ROLE_AGENT, ParsedTranscript, agent_value,
                     as_int, as_number, clean_text, iso, parse_iso, participants_map,
                     periods, role_for_item, strip_private, summary_from, user_value)

TEXT_TYPES = {"text/plain", "text/markdown"}


def parse_chat(doc: dict, started_at: datetime,
               agent_roles: Iterable[str] = DEFAULT_AGENT_ROLES) -> ParsedTranscript:
    parts = participants_map(doc)
    turns: list[dict] = []
    skipped_events = 0
    skipped_non_text = 0
    agent_ids_seen: list[str] = []
    escalation_at = None
    first_ts = last_ts = None

    for item in doc.get("Transcript") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("Type") or "MESSAGE").upper() != "MESSAGE":
            skipped_events += 1
            continue
        ctype = str(item.get("ContentType") or "text/plain").lower()
        if ctype not in TEXT_TYPES:
            skipped_non_text += 1
            continue
        text = clean_text(item.get("Content"))
        if not text:
            continue

        role = role_for_item(item, parts, agent_roles)
        ts = parse_iso(item["AbsoluteTime"]) if item.get("AbsoluteTime") else started_at
        pid = str(item.get("ParticipantId") or role)

        if role == ROLE_AGENT and pid not in agent_ids_seen:
            agent_ids_seen.append(pid)
            if len(agent_ids_seen) == 2 and escalation_at is None:
                escalation_at = ts

        first_ts = first_ts or ts
        last_ts = ts
        turns.append({"role": role, "text": text, "ts": iso(ts), "_pid": pid})

    cc = doc.get("ConversationCharacteristics") or {}
    duration = as_int(cc.get("TotalConversationDurationMillis"))
    if duration is None and first_ts and last_ts:
        duration = int((last_ts - first_ts).total_seconds() * 1000)

    participant_ids = list(parts) or sorted({t["_pid"] for t in turns})
    return ParsedTranscript(
        turns=strip_private(turns),
        participant_ids=participant_ids,
        duration_ms=duration,
        platform_signals=_signals(cc, parts, agent_roles) or None,
        escalation_at=escalation_at,
        notes={"events_skipped": skipped_events, "non_text_items_skipped": skipped_non_text},
    )


def _signals(cc: dict, parts: dict, agent_roles: Iterable[str]) -> dict:
    out: dict = {}
    sentiment = cc.get("Sentiment") or {}
    overall_map = sentiment.get("OverallSentiment") or {}

    overall = as_number(user_value(overall_map, parts, agent_roles))
    if overall is None:
        # Chat files may split the score by interaction; prefer the with-agent value.
        with_agent = (overall_map.get("DetailsByInteraction") or {}).get("WithAgent")
        overall = as_number(user_value(with_agent, parts, agent_roles))
    if overall is not None:
        out["overall_sentiment_user"] = overall

    quarters = (sentiment.get("SentimentByPeriod") or {}).get("QUARTER")
    by_period = periods(user_value(quarters, parts, agent_roles))
    if by_period:
        out["sentiment_by_period"] = by_period

    shift = user_value(sentiment.get("SentimentShift"), parts, agent_roles)
    if isinstance(shift, dict):
        begin, end = as_number(shift.get("BeginScore")), as_number(shift.get("EndScore"))
        if begin is not None and end is not None:
            out["sentiment_shift_user"] = {"begin": begin, "end": end}

    rt = cc.get("ResponseTime") or {}
    agent_rt = agent_value(rt.get("DetailsByParticipant"), parts, agent_roles) or {}
    avg = None
    for key in ("AverageMillis", "AverageResponseTimeMillis", "Average"):
        avg = as_int(agent_rt.get(key)) if isinstance(agent_rt, dict) else None
        if avg is not None:
            break
    if avg is None:
        avg = as_int(rt.get("AgentGreetingTimeMillis"))
    if avg is not None:
        out["response_time_ms"] = avg

    talk = as_int((cc.get("TalkTime") or {}).get("TotalTimeMillis"))
    if talk is not None:
        out["talk_time_ms"] = talk
    non_talk = as_int((cc.get("NonTalkTime") or {}).get("TotalTimeMillis"))
    if non_talk is not None:
        out["non_talk_time_ms"] = non_talk

    summary = summary_from(cc)
    if summary:
        out["summary"] = summary
    return out
