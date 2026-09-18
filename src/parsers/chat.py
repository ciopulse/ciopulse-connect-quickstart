"""Parser for Amazon Connect chat analytics output (Version CHAT-2022-11-30).

Shape (redacted file): Transcript[] items with absolute timestamps, a Type
(MESSAGE or EVENT) and a ContentType. Only text/plain and text/markdown items
are conversation turns; joins, leaves and attachments are skipped. Character
offsets (used by Connect for highlights) are irrelevant to the contract and
ignored. ConversationCharacteristics adds ResponseTime and SentimentShift, keyed
by participant role. Flow and bot messages arrive with ParticipantRole SYSTEM
(verified on a live instance, Sep 2026), which is why SYSTEM is an agent role
by default.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .common import (DEFAULT_AGENT_ROLES, ROLE_AGENT, ROLE_USER, ParsedTranscript, actor_of, agent_value,
                     as_int, as_number, clean_text, iso, parse_iso, participants_map, periods,
                     raw_role_for_item, role_for_item, role_of, strip_private, summary_from, user_value)

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
        turn = {"role": role, "text": text, "ts": iso(ts), "_pid": pid}
        actor = actor_of(raw_role_for_item(item, parts)) if role == ROLE_AGENT else None
        if actor:
            turn["actor"] = actor
        turns.append(turn)

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


def _by_role(block, key_new="DetailsByParticipantRole", key_old="DetailsByParticipant"):
    """Connect nests per-participant data under DetailsByParticipantRole (current) or DetailsByParticipant / the map itself (older)."""
    if not isinstance(block, dict):
        return {}
    for k in (key_new, key_old):
        if isinstance(block.get(k), dict):
            return block[k]
    return block


def _signals(cc: dict, parts: dict, agent_roles: Iterable[str]) -> dict:
    out: dict = {}
    sentiment = cc.get("Sentiment") or {}

    # Overall customer sentiment: DetailsByParticipantRole.CUSTOMER (current shape), a flat CUSTOMER key
    # (older shape), then the per-interaction split (WithAgent preferred, WithoutAgent for bot-only chats).
    overall_map = sentiment.get("OverallSentiment") or {}
    overall = as_number(user_value(_by_role(overall_map), parts, agent_roles))
    if overall is None:
        dbi = overall_map.get("DetailsByInteraction") or {}
        per_interaction = user_value(_by_role(dbi), parts, agent_roles)
        if isinstance(per_interaction, dict):        # current: ...DetailsByParticipantRole.CUSTOMER.{WithAgent,WithoutAgent}
            overall = as_number(per_interaction.get("WithAgent"))
            if overall is None:
                overall = as_number(per_interaction.get("WithoutAgent"))
        if overall is None:                          # older: ...DetailsByInteraction.{WithAgent,WithoutAgent}.CUSTOMER
            for split in ("WithAgent", "WithoutAgent"):
                overall = as_number(user_value(dbi.get(split), parts, agent_roles))
                if overall is not None:
                    break
    if overall is not None:
        out["overall_sentiment_user"] = overall

    # By period: QUARTER buckets where present (voice-style); otherwise the customer's progressive score
    # after each of their message groups, which is what chat analysis actually emits.
    quarters = (sentiment.get("SentimentByPeriod") or {}).get("QUARTER")
    by_period = periods(user_value(_by_role(quarters or {}), parts, agent_roles)) if quarters else None
    if not by_period:
        groups = sentiment.get("DetailsByTranscriptItemGroup")
        if isinstance(groups, list):
            scores = [as_number(g.get("ProgressiveScore")) for g in groups if isinstance(g, dict)
                      and role_of(None, g.get("ParticipantRole"), parts, agent_roles) == ROLE_USER]
            scores = [x for x in scores if x is not None]
            if scores:
                by_period = [{"period": i + 1, "score": x} for i, x in enumerate(scores)]
    if by_period:
        out["sentiment_by_period"] = by_period

    shift = user_value(_by_role(sentiment.get("SentimentShift") or {}), parts, agent_roles)
    if isinstance(shift, dict):
        begin, end = as_number(shift.get("BeginScore")), as_number(shift.get("EndScore"))
        if begin is not None and end is not None:
            out["sentiment_shift_user"] = {"begin": begin, "end": end}

    # Agent response time: Average.ValueMillis (current) or AverageMillis (older); greeting time as fallback.
    rt = cc.get("ResponseTime") or {}
    agent_rt = agent_value(_by_role(rt), parts, agent_roles)
    avg = None
    if isinstance(agent_rt, dict):
        avg = as_int((agent_rt.get("Average") or {}).get("ValueMillis")) if isinstance(agent_rt.get("Average"), dict) else None
        if avg is None:
            for key in ("AverageMillis", "AverageResponseTimeMillis", "Average"):
                avg = as_int(agent_rt.get(key))
                if avg is not None:
                    break
    if avg is None:
        avg = as_int(rt.get("AgentGreetingTimeMillis")) or as_int(rt.get("AutomatedInteractionGreetingTimeMillis"))
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
