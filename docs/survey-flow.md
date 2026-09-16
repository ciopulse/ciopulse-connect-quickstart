# Post-conversation survey from Amazon Connect

This is the second lens: after a voice call, Connect sends the caller an SMS with a link to a ciopulse survey. For chat, the bot puts the same link in its goodbye message. ciopulse never sees the phone number; Connect sends the message and the link carries the contact ID, which is how the survey answer joins the transcript the forwarder sent.

No code is involved. Everything below is done in the Connect console by a flow administrator. Budget an hour for the flow work, and start the phone-number step first because it is the long pole.

---

## Before you start

1. **An SMS-capable origination number claimed to the instance.** Connect console → *Channels* → *Phone numbers* → *Claim a number* → choose a number with SMS capability, or port one. Outbound SMS from Connect uses AWS End User Messaging behind the scenes and the number must be registered with carriers. **In Australia, carrier registration takes weeks, not days.** Raise it at the start of the project.
2. **Your survey link from ciopulse.** It has the form

   ```
   https://<your ciopulse host>/survey?tid=<contact id>&rgid=<group code>&agid=<agent id>
   ```

   ciopulse gives you the host, the group code (`rgid`) and the agent id (`agid`) at onboarding. `tid` is filled in by Connect per contact.
3. **A Disconnect flow** attached to the inbound flow that handles the AI agent. Connect runs it after the customer hangs up, which is the right moment to send the survey.

## Voice: the Disconnect flow

Open (or create) the Disconnect flow and add these blocks in order.

### 1. `Distribute by percentage` (optional sampling)

Add the block first, with one branch at **100%** going to the next step and the remainder to *End flow*. Start at 100%. Lowering it later is a one-field change and does not touch anything else. If you never want sampling, skip the block.

### 2. `Set contact attributes`

- **Namespace:** *User defined*
- **Attribute:** `survey_url`
- **Value:** your survey link with the contact ID inserted. Set the value type to *Dynamic* is **not** needed here: enter the URL as plain text and use the system attribute reference inside it:

  ```
  https://<your ciopulse host>/survey?tid=$.InitialContactId&rgid=<group code>&agid=<agent id>
  ```

  Use `$.InitialContactId`, not `$.ContactId`. When an agent transfers the contact, Connect mints a new contact ID for the second leg and the Disconnect flow runs on that last leg. `InitialContactId` always points at the first leg, where the AI agent was, and equals the contact's own ID when there was no transfer. The forwarder sends each leg with its own ID as `session_id` and carries `initial_contact_id` in metadata, so ciopulse joins the survey to the whole conversation.

Why an attribute rather than writing the URL straight into the message: AWS documents message templates as static text with dynamic content supplied through user-defined attributes. Writing the full URL into an attribute first is the pattern AWS confirms, and it also makes the link visible in the contact record for troubleshooting.

### 3. `Send message`

- **Channel:** SMS
- **From:** the SMS-capable number claimed in step 1
- **To:** the customer's number. Use the system attribute `$.CustomerEndpoint.Address`.
- **Message body:** plain text, **under 1,024 characters**, referencing the attribute. For example:

  ```
  Thanks for calling IT support. We would value 30 seconds of your feedback: $.Attributes.survey_url
  ```

  Keep it short. Do not include the caller's name; the contact was analysed with redaction on and the message should not carry anything the transcript does not.

- **Error branch:** route to *End flow*. A failed SMS should not affect the contact.

### 4. `Disconnect / hang up`

Save and **publish** the flow. Then make one test call from a mobile and check the SMS arrives with a working link.

## Chat: the bot's goodbye

For chat there is no SMS. Put the same link in the last message the bot sends. Where the bot is built in Amazon Lex or as a Connect AI agent, add the link to the closing response and use the contact ID as `tid` exactly as above:

```
Thanks for chatting. Tell us how it went: https://<your ciopulse host>/survey?tid=<contact id>&rgid=<group code>&agid=<agent id>
```

If the bot cannot access the contact ID at that point, pass it in from the contact flow as a session attribute before the bot block. The essential rule is unchanged: the `tid` on the link must equal the contact ID the forwarder sends as `session_id`, or the two lenses will not join.

## Checks

| Check | How |
|---|---|
| The SMS arrives | Test call from a mobile. If not, check the number's SMS capability and registration status before the flow |
| The link opens the survey | Tap it. A wrong `rgid` or `agid` shows a ciopulse error page |
| The survey joins the transcript | In ciopulse, the conversation should show both the survey score and the transcript reading within a few minutes of the call |
| Desk-phone callers | An SMS to a desk phone is silently lost. If a large share of your callers use desk phones, sample from mobile-originated contacts only, or accept the gap |

## Frequency

ciopulse recommends no more than one survey per person every four weeks. Connect does not enforce that; if you need it, gate the `Send message` block on a Lambda that checks the caller's last survey date in a store you own.
