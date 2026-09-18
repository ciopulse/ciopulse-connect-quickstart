"""Parser for Amazon Connect voice analytics output.

Shape (redacted file): Transcript[] segments with millisecond offsets relative
to the start of the analysed audio, and ConversationCharacteristics carrying
sentiment, talk time, non-talk time, interruptions and the post-contact summary.

Consecutive segments from the same speaker are merged into one turn: a spoken
turn is often delivered as several short segments, and the contract counts
turns (max 500), not segments.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from .common import (DEFAULT_AGENT_ROLES, ROLE_AGENT, ParsedTranscript, actor_of, agent_value,
                     as_int, as_number, clean_text, iso, participants_map, periods,
                     raw_role_for_item, role_for_item, strip_private, summary_from, user_value)


def parse_voice(doc: dict, started_at: datetime,
                agent_roles: Iterable[str] = DEFAULT_AGENT_ROLES) -> ParsedTranscript:
    parts = participants_map(doc)
    turns: list[dict] = []
    merged = 0
    agent_ids_seen: list[str] = []
    escalation_at = None

    for seg in doc.get("Transcript") or []:
        if not isinstance(seg, dict):
            continue
        text = clean_text(seg.get("Content"))
        if not text:
            continue
        role = role_for_item(seg, parts, agent_roles)
        offset = as_int(seg.get("BeginOffsetMillis")) or 0
        ts = started_at + timedelta(milliseconds=offset)
        pid = str(seg.get("ParticipantId") or role)

        if role == ROLE_AGENT and pid not in agent_ids_seen:
            agent_ids_seen.append(pid)
            if len(agent_ids_seen) == 2 and escalation_at is None:
                escalation_at = ts

        if turns and turns[-1]["role"] == role and turns[-1]["_pid"] == pid:
            turns[-1]["text"] += " " + text
            merged += 1
        else:
            turn = {"role": role, "text": text, "ts": iso(ts), "_pid": pid}
            actor = actor_of(raw_role_for_item(seg, parts)) if role == ROLE_AGENT else None
            if actor:
                turn["actor"] = actor
            turns.append(turn)

    cc = doc.get("ConversationCharacteristics") or {}
    duration = as_int(cc.get("TotalConversationDurationMillis"))
    signals = _signals(cc, parts, agent_roles)

    participant_ids = list(parts) or sorted({t["_pid"] for t in turns})
    return ParsedTranscript(
        turns=strip_private(turns),
        participant_ids=participant_ids,
        duration_ms=duration,
        platform_signals=signals or None,
        escalation_at=escalation_at,
        notes={"segments_merged": merged},
    )


def _signals(cc: dict, parts: dict, agent_roles: Iterable[str]) -> dict:
    out: dict = {}
    sentiment = cc.get("Sentiment") or {}

    overall = as_number(user_value(sentiment.get("OverallSentiment"), parts, agent_roles))
    if overall is not None:
        out["overall_sentiment_user"] = overall

    quarters = (sentiment.get("SentimentByPeriod") or {}).get("QUARTER")
    by_period = periods(user_value(quarters, parts, agent_roles))
    if by_period:
        out["sentiment_by_period"] = by_period

    talk = as_int((cc.get("TalkTime") or {}).get("TotalTimeMillis"))
    if talk is not None:
        out["talk_time_ms"] = talk
    non_talk = as_int((cc.get("NonTalkTime") or {}).get("TotalTimeMillis"))
    if non_talk is not None:
        out["non_talk_time_ms"] = non_talk
    interruptions = as_int((cc.get("Interruptions") or {}).get("TotalCount"))
    if interruptions is not None:
        out["interruptions"] = interruptions

    summary = summary_from(cc)
    if summary:
        out["summary"] = summary
    return out
