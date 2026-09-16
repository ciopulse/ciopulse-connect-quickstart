#!/usr/bin/env python3
"""Drive one customer-side test chat against a Connect flow, entirely through the API.

    pip install boto3 websockets
    python3 tools/sandbox_chat.py <instance-id> <contact-flow-id> [region]
    SANDBOX_REPLIES='["first reply", "second reply"]' python3 tools/sandbox_chat.py ...   # custom script
    SANDBOX_TIMEOUT=600 python3 tools/sandbox_chat.py ...   # keep the customer side open longer (agent tests)

No Connect user or CCP is needed: the script is the customer, the flow is the
bot. It joins over the websocket (which is what starts the flow), prints every
transcript item as it arrives, sends a scripted reply after each bot prompt,
and exits when the flow disconnects. Contact Lens output appears in the
analytics bucket a few minutes later. Synthetic text only.
"""
import asyncio
import json
import os
import sys
import time

import boto3
import websockets

REPLIES = json.loads(os.environ["SANDBOX_REPLIES"]) if os.environ.get("SANDBOX_REPLIES") else [
    "hi, I can't get onto the VPN, it keeps saying authentication failed and I've tried three times already. "
    "my email is sandbox.tester@example.com",
    "yep that worked, thanks a lot",
]


async def run(instance_id: str, flow_id: str, region: str) -> str:
    connect = boto3.client("connect", region_name=region)
    cp = boto3.client("connectparticipant", region_name=region)
    start = connect.start_chat_contact(
        InstanceId=instance_id, ContactFlowId=flow_id,
        ParticipantDetails={"DisplayName": "Sandbox Tester"}, Attributes={"sandbox": "true"},
        SupportedMessagingContentTypes=["text/plain", "text/markdown"])
    contact_id = start["ContactId"]
    print("ContactId", contact_id, flush=True)
    conn = cp.create_participant_connection(ParticipantToken=start["ParticipantToken"],
                                            Type=["WEBSOCKET", "CONNECTION_CREDENTIALS"])
    token = conn["ConnectionCredentials"]["ConnectionToken"]
    replies, seen, bot_messages = list(REPLIES), set(), 0

    async with websockets.connect(conn["Websocket"]["Url"], max_size=2 ** 20) as ws:
        await ws.send(json.dumps({"topic": "aws/subscribe", "content": {"topics": ["aws/chat"]}}))
        deadline = time.time() + int(os.environ.get("SANDBOX_TIMEOUT", "180"))
        while time.time() < deadline:
            try:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            except asyncio.TimeoutError:
                print("  (no frame for 30s)", flush=True)
                continue
            if frame.get("topic") != "aws/chat":
                continue
            item = json.loads(frame["content"])
            if item.get("Id") in seen:
                continue
            seen.add(item.get("Id"))
            role = item.get("ParticipantRole") or "-"
            typ = item.get("Type") or "-"
            ctype = item.get("ContentType") or ""
            print(f"  {item.get('AbsoluteTime', '')[11:23]} {role:<8} {typ:<8} {ctype.split('.')[-1]:<20} "
                  f"{item.get('Content', '')[:80]!r}", flush=True)
            if typ == "EVENT" and ctype.endswith("chat.ended"):
                break
            if typ == "MESSAGE" and role != "CUSTOMER":
                bot_messages += 1
                if replies:
                    await asyncio.sleep(1.5)
                    cp.send_message(ConnectionToken=token, ContentType="text/plain", Content=replies.pop(0))
                    print("  -> customer reply sent", flush=True)
    return contact_id


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cid = asyncio.run(run(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "ap-southeast-2"))
    print("done; contact", cid, "- analytics output appears in the bucket in a few minutes")
