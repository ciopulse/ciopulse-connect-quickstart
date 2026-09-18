"""Shared helpers for the voice and chat parsers.

Both Connect analytics shapes are reduced to the same ParsedTranscript so the
handler can build one payload regardless of channel.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

ROLE_USER = "user"
ROLE_AGENT = "agent"
ROLE_SYSTEM = "system"

DEFAULT_AGENT_ROLES = frozenset({"AGENT", "BOT", "CUSTOM_BOT", "SYSTEM"})
BOT_RAW_ROLES = frozenset({"SYSTEM", "BOT", "CUSTOM_BOT"})
HUMAN_RAW_ROLES = frozenset({"AGENT", "SUPERVISOR"})
SUMMARY_MAX_CHARS = 2000

_WS = re.compile(r"\s+")


@dataclass
class ParsedTranscript:
    turns: list[dict] = field(default_factory=list)
    participant_ids: list[str] = field(default_factory=list)
    duration_ms: Optional[int] = None
    platform_signals: Optional[dict] = None
    escalation_at: Optional[datetime] = None
    notes: dict = field(default_factory=dict)


def parse_agent_roles(csv: str | None) -> frozenset[str]:
    roles = {r.strip().upper() for r in (csv or "").split(",") if r.strip()}
    return frozenset(roles) if roles else DEFAULT_AGENT_ROLES


def participants_map(doc: dict) -> dict[str, str]:
    """ParticipantId -> ParticipantRole from the file's Participants array."""
    out: dict[str, str] = {}
    for p in doc.get("Participants") or []:
        if isinstance(p, dict) and p.get("ParticipantId"):
            out[str(p["ParticipantId"])] = str(p.get("ParticipantRole") or "").upper()
    return out


def role_of(participant_id: Any, participant_role: Any, parts: dict[str, str],
            agent_roles: Iterable[str]) -> str:
    """Map a Connect participant to a contract role.

    Preference order: the item's own ParticipantRole, then the Participants
    array, then the ParticipantId itself (voice files use AGENT/CUSTOMER as ids).
    """
    raw = participant_role or parts.get(str(participant_id or "")) or participant_id or ""
    raw = str(raw).upper()
    if raw == "CUSTOMER":
        return ROLE_USER
    if raw in set(agent_roles):
        return ROLE_AGENT
    return ROLE_SYSTEM


def role_for_item(item: dict, parts: dict[str, str], agent_roles: Iterable[str]) -> str:
    return role_of(item.get("ParticipantId"), item.get("ParticipantRole"), parts, agent_roles)


def raw_role_for_item(item: dict, parts: dict[str, str]) -> str:
    """Connect's own label for the speaker (CUSTOMER, AGENT, SYSTEM, ...), before contract mapping."""
    raw = item.get("ParticipantRole") or parts.get(str(item.get("ParticipantId") or "")) or item.get("ParticipantId") or ""
    return str(raw).upper()


def actor_of(raw_role: str) -> Optional[str]:
    """Contract v0.3 turns[].actor: who produced an agent turn. None when unknown."""
    if raw_role in BOT_RAW_ROLES:
        return "bot"
    if raw_role in HUMAN_RAW_ROLES:
        return "human"
    return None


def clean_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return _WS.sub(" ", text).strip()


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def parse_iso(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def user_value(mapping: Any, parts: dict[str, str], agent_roles: Iterable[str]) -> Any:
    """From a per-participant mapping (keyed by role or id), return the customer's entry."""
    if not isinstance(mapping, dict):
        return None
    for key, value in mapping.items():
        if role_of(key, None, parts, agent_roles) == ROLE_USER:
            return value
    return None


def agent_value(mapping: Any, parts: dict[str, str], agent_roles: Iterable[str]) -> Any:
    if not isinstance(mapping, dict):
        return None
    for key, value in mapping.items():
        if role_of(key, None, parts, agent_roles) == ROLE_AGENT:
            return value
    return None


def as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        # Some shapes nest the score one level down.
        for k in ("Score", "Value"):
            if isinstance(value.get(k), (int, float)) and not isinstance(value.get(k), bool):
                return value[k]
    return None


def as_int(value: Any) -> Optional[int]:
    n = as_number(value)
    return int(n) if n is not None else None


def periods(value: Any) -> Optional[list[dict]]:
    """SentimentByPeriod entries -> [{period: n, score: x}, ...]."""
    if not isinstance(value, list):
        return None
    out = []
    for i, entry in enumerate(value):
        score = as_number(entry.get("Score") if isinstance(entry, dict) else None)
        if score is not None:
            out.append({"period": i + 1, "score": score})
    return out or None


def summary_from(cc: dict) -> Optional[str]:
    """Post-contact summary text. AWS has published two nestings; accept both."""
    cs = cc.get("ContactSummary")
    if not isinstance(cs, dict):
        return None
    candidates = [
        (((cs.get("AutoGenerated") or {}).get("PostContactSummary")) or {}).get("Content"),
        (cs.get("PostContactSummary") or {}).get("Content"),
    ]
    for c in candidates:
        text = clean_text(c)
        if text:
            return text[:SUMMARY_MAX_CHARS]
    return None


def strip_private(turns: list[dict]) -> list[dict]:
    return [{k: v for k, v in t.items() if not k.startswith("_")} for t in turns]
