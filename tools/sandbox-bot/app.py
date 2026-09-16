"""Lex V2 code hook: a generative IT service-desk bot backed by a model on Amazon Bedrock.

Every customer utterance lands on the Lex FallbackIntent (or TalkToHuman), which
invokes this function. The conversation history lives in Lex session attributes,
so the bot is stateless. The model is asked to append [ESCALATE] when the caller
wants a person and [DONE] when the conversation is finished; those become the
session attributes the Connect flow branches on. Sandbox use only: synthetic
scenarios, no real user data, no logging of transcript text.
"""
from __future__ import annotations

import json
import os

import boto3

MODEL_ID = os.environ.get("MODEL_ID", "apac.amazon.nova-lite-v1:0")
MAX_HISTORY = 16
HUMAN_WORDS = ("human", "person", "agent", "technician", "someone", "real ", "operator", "useless")
DONE_WORDS = ("thanks", "thank you", "that worked", "sorted", "all good", "that's all", "nothing else", "bye", "cheers", "fixed")
SYSTEM_PROMPT = """You are the AI assistant on an internal IT service desk phone and chat line for a large company.
Your replies are spoken aloud by a voice system, so keep each reply to one or two short sentences, plain words, no lists, no markdown.
You can help with: password resets (you send a reset link to the person's registered email), VPN problems (you can renew an expired VPN certificate, it installs in about two minutes), laptop and Wi-Fi issues, and checking the status of an existing ticket (invent a plausible generic status; never invent names of real people).
Ask at most one clarifying question before acting. Confirm what you did and tell the person what to expect.
Control tokens, used sparingly and only at the very end of a reply:
- [ESCALATE]: only when the person explicitly asks for a human, a person, an agent or a technician, or when they say your fix did not work for the second time. Say you will transfer them now, then the token.
- [DONE]: only after the person has said the problem is fixed or that they have nothing else. Say a short goodbye, then the token.
Never use a token in your first reply. Never use both tokens. When unsure, use no token and keep helping.
Never ask for passwords, card numbers, or identity numbers."""

_bedrock = boto3.client("bedrock-runtime")


def _lex_response(event, attrs, reply, close: bool):
    intent = (event.get("sessionState") or {}).get("intent") or {"name": "FallbackIntent"}
    state = {"sessionAttributes": attrs}
    if close:
        state["dialogAction"] = {"type": "Close"}
        state["intent"] = {"name": intent.get("name", "FallbackIntent"), "state": "Fulfilled"}
    else:
        state["dialogAction"] = {"type": "ElicitIntent"}
    return {"sessionState": state, "messages": [{"contentType": "PlainText", "content": reply}]}


def handler(event, context):
    session = event.get("sessionState") or {}
    attrs = dict(session.get("sessionAttributes") or {})
    intent_name = ((session.get("intent") or {}).get("name")) or "FallbackIntent"
    user_text = (event.get("inputTranscript") or "").strip()
    turns = int(attrs.get("turns", "0")) + 1
    attrs["turns"] = str(turns)

    if intent_name == "TalkToHuman":
        attrs["escalate"] = "true"
        return _lex_response(event, attrs, "No problem, I'll transfer you to a person now.", close=True)

    history = json.loads(attrs.get("history", "[]"))
    if user_text:
        history.append({"role": "user", "content": [{"text": user_text}]})
    if not history:
        history.append({"role": "user", "content": [{"text": "(the caller has connected but said nothing yet; greet them briefly)"}]})

    resp = _bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM_PROMPT}],
        messages=history[-MAX_HISTORY:],
        inferenceConfig={"maxTokens": 200, "temperature": 0.3},
    )
    reply = resp["output"]["message"]["content"][0]["text"].strip()
    escalate = "[ESCALATE]" in reply
    done = "[DONE]" in reply
    reply = reply.replace("[ESCALATE]", "").replace("[DONE]", "").strip() or "Let me transfer you to a person."
    # Guards against a model that reaches for the tokens too early (small models do).
    asked_for_person = any(w in user_text.lower() for w in HUMAN_WORDS)
    if turns == 1 and not asked_for_person:
        escalate = False
    if turns == 1 or not any(w in user_text.lower() for w in DONE_WORDS):
        done = False
    if escalate and done:
        done = False

    history.append({"role": "assistant", "content": [{"text": reply}]})
    attrs["history"] = json.dumps(history[-MAX_HISTORY:])
    attrs["escalate"] = "true" if escalate else "false"
    attrs["done"] = "true" if done else "false"
    print(json.dumps({"event": "bot_turn", "turn": turns, "intent": intent_name, "escalate": escalate, "done": done,
                      "in_chars": len(user_text), "out_chars": len(reply), "model": MODEL_ID}))
    return _lex_response(event, attrs, reply, close=escalate or done)
