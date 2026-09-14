"""Placeholder for the Kinesis real-time variant. Not built.

Amazon Connect's documented real-time path associates the
REAL_TIME_CONTACT_ANALYSIS_VOICE_SEGMENTS / ..._CHAT_SEGMENTS storage types
with a Kinesis Data Stream. The stream carries partial utterances, final
turns with sentiment, categories and the post-contact summary as they are
produced.

That path would let a forwarder deliver a transcript seconds after disconnect
rather than minutes. It is not needed for the survey-and-transcript join, and
it requires assembling a contact from many stream records, which the S3 path
avoids entirely. If you need it, the shape to build is:

    def kinesis_handler(event, context):
        for record in event["Records"]:
            segment = json.loads(base64.b64decode(record["kinesis"]["data"]))
            # accumulate by ContactId; on the PostContactSummary (or a
            # disconnect signal) build the same payload handler.build_payload
            # produces and post it with handler.post_with_retries.

Until then the S3 event path in handler.py is the supported one.
"""


def kinesis_handler(event, context):  # pragma: no cover
    raise NotImplementedError("Kinesis real-time forwarding is documented in this file but not built.")
