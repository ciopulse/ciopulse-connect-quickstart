"""Parser unit tests: each Connect shape -> ParsedTranscript, and the role/merge rules."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from conftest import CHAT_START, VOICE_START, load_fixture
from parsers.chat import parse_chat
from parsers.common import parse_agent_roles, role_of
from parsers.voice import parse_voice


def test_voice_turns_roles_and_merge():
    parsed = parse_voice(load_fixture("contact-lens-voice-redacted.json"), VOICE_START)
    roles = [t["role"] for t in parsed.turns]
    # nine segments, two consecutive customer segments merged -> eight turns
    assert len(parsed.turns) == 8
    assert roles == ["agent", "user", "agent", "user", "agent", "user", "agent", "user"]
    assert parsed.notes["segments_merged"] == 1
    assert parsed.turns[1]["text"].startswith("Yeah hi um") and parsed.turns[1]["text"].endswith("three times already")
    assert parsed.turns[3]["text"] == "it's [PII]"  # redaction marker passed through untouched


def test_voice_timestamps_are_offsets_from_started_at():
    parsed = parse_voice(load_fixture("contact-lens-voice-redacted.json"), VOICE_START)
    assert parsed.turns[0]["ts"] == "2026-09-02T00:14:00.000+00:00"
    assert parsed.turns[1]["ts"] == "2026-09-02T00:14:03.800+00:00"
    assert [t["ts"] for t in parsed.turns] == sorted(t["ts"] for t in parsed.turns)
    assert parsed.duration_ms == 45000


def test_voice_platform_signals_generic_names_only():
    parsed = parse_voice(load_fixture("contact-lens-voice-redacted.json"), VOICE_START)
    sig = parsed.platform_signals
    assert sig["overall_sentiment_user"] == -0.5
    assert [p["score"] for p in sig["sentiment_by_period"]] == [-2.5, -1.0, 0.0, 2.5]
    assert sig["talk_time_ms"] == 31000 and sig["non_talk_time_ms"] == 9000 and sig["interruptions"] == 1
    assert sig["summary"].startswith("Customer could not connect to the VPN")
    assert not any("lens" in k.lower() or "contact" in k.lower() for k in sig)


def test_voice_without_analytics_block_has_no_signals():
    doc = load_fixture("contact-lens-voice-redacted.json")
    del doc["ConversationCharacteristics"]
    parsed = parse_voice(doc, VOICE_START)
    assert parsed.platform_signals is None
    assert parsed.duration_ms is None
    assert len(parsed.turns) == 8


def test_voice_summary_accepts_older_nesting():
    doc = load_fixture("contact-lens-voice-redacted.json")
    doc["ConversationCharacteristics"]["ContactSummary"] = {"PostContactSummary": {"Content": "older shape"}}
    assert parse_voice(doc, VOICE_START).platform_signals["summary"] == "older shape"


def test_voice_escalation_detected_when_second_agent_participant_speaks():
    doc = load_fixture("contact-lens-voice-redacted.json")
    doc["Participants"].append({"ParticipantId": "AGENT-2", "ParticipantRole": "AGENT"})
    doc["Transcript"].append({"ParticipantId": "AGENT-2", "ParticipantRole": "AGENT",
                              "Content": "Hi, this is the network team.", "BeginOffsetMillis": 44000})
    parsed = parse_voice(doc, VOICE_START)
    assert parsed.escalation_at == VOICE_START.replace(second=44)
    assert len(parsed.participant_ids) == 3


def test_chat_real_shape_turns_and_roles():
    """Fixture is a sanitised file from a live instance: bot-only chat, bot messages carry SYSTEM."""
    parsed = parse_chat(load_fixture("contact-lens-chat-redacted.json"), CHAT_START)
    assert [t["role"] for t in parsed.turns] == ["agent", "agent", "user", "agent"]
    assert parsed.notes == {"events_skipped": 2, "non_text_items_skipped": 0}
    assert parsed.turns[0]["ts"] == "2026-09-15T01:45:53.902+00:00"
    assert parsed.duration_ms == 5139  # no TotalConversationDurationMillis in chat files; first->last message
    assert len(parsed.participant_ids) == 2


def test_chat_real_shape_platform_signals():
    sig = parse_chat(load_fixture("contact-lens-chat-redacted.json"), CHAT_START).platform_signals
    assert sig["overall_sentiment_user"] == -5                       # OverallSentiment.DetailsByParticipantRole.CUSTOMER
    assert sig["sentiment_by_period"] == [{"period": 1, "score": -5}]  # DetailsByTranscriptItemGroup progressive score
    assert sig["response_time_ms"] == 706                            # ResponseTime.DetailsByParticipantRole.SYSTEM.Average.ValueMillis
    assert "sentiment_shift_user" not in sig                         # customer shift block is empty in the file
    assert "summary" not in sig and "talk_time_ms" not in sig


def test_chat_system_not_agent_when_operator_says_so():
    parsed = parse_chat(load_fixture("contact-lens-chat-redacted.json"), CHAT_START, parse_agent_roles("AGENT,CUSTOM_BOT"))
    assert [t["role"] for t in parsed.turns] == ["system", "system", "user", "system"]
    # no agent-role participant, so response time falls back to the bot greeting time
    assert parsed.platform_signals["response_time_ms"] == 1958


def test_chat_synthetic_skips_events_and_attachments():
    parsed = parse_chat(load_fixture("contact-lens-chat-synthetic.json"), CHAT_START)
    assert [t["role"] for t in parsed.turns] == ["user", "agent", "user", "agent", "user"]
    assert parsed.notes == {"events_skipped": 2, "non_text_items_skipped": 1}
    assert parsed.turns[3]["text"] == "Great. Anything else I can help with?"  # text/markdown kept


def test_chat_synthetic_older_shapes_still_parse():
    sig = parse_chat(load_fixture("contact-lens-chat-synthetic.json"), CHAT_START).platform_signals
    assert sig["overall_sentiment_user"] == 0.5
    assert sig["sentiment_shift_user"] == {"begin": -2.0, "end": 3.0}
    assert sig["response_time_ms"] == 6200
    assert [p["score"] for p in sig["sentiment_by_period"]] == [-2.0, -0.5, 1.0, 3.0]
    assert "talk_time_ms" not in sig


def test_chat_sentiment_falls_back_to_with_agent_split():
    doc = load_fixture("contact-lens-chat-synthetic.json")
    doc["ConversationCharacteristics"]["Sentiment"]["OverallSentiment"] = {
        "DetailsByInteraction": {"WithAgent": {"CUSTOMER": -1.5}, "WithoutAgent": {"CUSTOMER": 2.0}}}
    assert parse_chat(doc, CHAT_START).platform_signals["overall_sentiment_user"] == -1.5


def test_chat_duration_falls_back_to_first_and_last_turn():
    doc = load_fixture("contact-lens-chat-synthetic.json")
    del doc["ConversationCharacteristics"]["TotalConversationDurationMillis"]
    assert parse_chat(doc, CHAT_START).duration_ms == 163000


def test_role_mapping_rules():
    parts = {"p1": "CUSTOMER", "p2": "AGENT", "p3": "SYSTEM", "p4": "CUSTOM_BOT", "p5": "SUPERVISOR"}
    roles = parse_agent_roles("AGENT,BOT,CUSTOM_BOT")
    assert role_of("p1", None, parts, roles) == "user"
    assert role_of("p2", None, parts, roles) == "agent"
    assert role_of("p3", None, parts, roles) == "system"
    assert role_of("p3", None, parts, parse_agent_roles("")) == "agent"  # default includes SYSTEM
    assert role_of("p4", None, parts, roles) == "agent"
    assert role_of("p5", None, parts, roles) == "system"
    assert role_of("CUSTOMER", None, {}, roles) == "user"       # voice ids double as roles
    assert role_of("AGENT", None, {}, roles) == "agent"
    assert role_of("p3", "CUSTOMER", parts, roles) == "user"    # item's own role wins
    assert role_of("p3", None, parts, parse_agent_roles("SYSTEM")) == "agent"  # operator can remap


def test_parse_agent_roles_defaults_when_blank():
    assert parse_agent_roles("") == frozenset({"AGENT", "BOT", "CUSTOM_BOT", "SYSTEM"})
    assert parse_agent_roles(" agent , custom_bot ") == frozenset({"AGENT", "CUSTOM_BOT"})
