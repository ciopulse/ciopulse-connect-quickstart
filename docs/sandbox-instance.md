# A sandbox Connect instance for trying the quickstart

If you do not have an Amazon Connect instance yet, this creates a throwaway one that produces real redacted analytics files, so you can deploy the forwarder and watch a transcript arrive end to end. Nothing here is production configuration.

Cost: the instance itself is free. You pay per contact minute for chat and voice, per minute for conversational analytics, and a monthly fee for any phone number you claim. A few dozen test chats cost cents.

## 1. Sign in to AWS on the CLI

```bash
aws login
```

Confirm with `aws sts get-caller-identity`. The scripts use whatever account and region the CLI is signed into.

## 2. Run the script

```bash
tools/create-sandbox-instance.sh ciopulse-sandbox ap-southeast-2
```

It creates the instance, an S3 bucket, the two storage associations, switches Contact Lens on at instance level, and enables EventBridge notifications on the bucket. Re-running is safe. It prints the two values you need for `sam deploy --guided`.

## 3. Get into the admin console

The script prints the instance access URL. On first use there is no admin user, so use the **emergency access** link: AWS console → Amazon Connect → your instance → *Log in for emergency access*. Then create yourself an admin user under *Users* so you do not need it again.

## 4. Enable analytics on the inbound flow

Analytics is a flow-level setting, not just an instance one. In the admin console:

1. *Routing* → *Flows* → open **Sample inbound flow (first contact experience)** or create a new inbound flow.
2. Add a **Set recording and analytics behavior** block right after the entry point.
   - Call recording: *Agent and customer*.
   - Analytics: **On**, language *English (Australia)* or your language, speech analytics *post-call*.
   - Sensitive data redaction: **On**, output **Redacted only**. This is the `redaction_option: RedactedOnly` behaviour; it means no unredacted transcript is ever written.
3. Route to a queue with an agent (the *BasicQueue* and your own user are fine for a sandbox) or, for a first test, `Play prompt` → `Transfer to queue`.
4. **Save and publish.**

Set the flow as the entry flow for chat (*Test chat* page picks it up) and for any claimed number.

## 5. Make a test contact

**Chat, no phone number needed.** Admin console → *Dashboard* → *Test chat*. Open the agent side (CCP) in a second tab, accept the chat, exchange a few messages, end it. This is the quickest way to see a file land, and it verifies the chat prefix the forwarder assumes.

**Voice** needs a claimed number: *Channels* → *Phone numbers* → *Claim a number*. In Australia, DID numbers require business identity documents and take days to provision; toll-free is quicker. Call it from your mobile, answer in the CCP, talk for thirty seconds, hang up.

Analytics output appears one to three minutes after the contact ends:

```bash
aws s3 ls s3://<bucket>/connect/ciopulse-sandbox/ --recursive | grep Analysis
```

The forwarder's `VoiceKeyPattern` and `ChatKeyPattern` defaults expect `…/Analysis/Voice/Redacted/…` and `…/Analysis/Chat/Redacted/…`. If the chat files land somewhere else, that is the undocumented prefix from RUN-LOG item 2; set `ChatKeyPattern` to match and note it.

## 6. Deploy the forwarder against it

```bash
aws secretsmanager create-secret --name ciopulse/api-key --secret-string '<key>'
sam build && sam deploy --guided
```

Use the bucket and instance ARN the script printed, the secret's ARN, and any agent id and version. Point `CiopulseEndpoint` at a mock receiver first if you want to see the payload before it reaches ciopulse.

## 7. Tear down

```bash
aws connect delete-instance --instance-id <id>
aws s3 rb s3://<bucket> --force
sam delete
```
