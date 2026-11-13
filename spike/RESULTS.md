# DM API spike results

Run on 13 Nov 2026 with a pay-per-use X developer app, OAuth 2.0 (PKCE) and scopes
`tweet.read users.read follows.read dm.read dm.write offline.access`.
Script: [`spike.py`](spike.py). Raw responses were logged locally and are not committed.

## Accounts

| Account | Role | State |
| --- | --- | --- |
| A | Sender (stands in for the outreach account) | Not verified, no Premium, X Chat passcode enabled |
| B | Warm recipient | Follows A, already had a conversation with A, X Chat enabled |
| C | Cold recipient | Never messaged A, does not follow A, X Chat enabled |

## What happened

| Case | `receives_your_dm` | Send result | Where it landed |
| --- | --- | --- | --- |
| B follows A, DMs open | `true` | 201 | B's inbox |
| B unfollows A, requests "No one", existing conversation | `true` | 201 | Same conversation as before |
| C requests "No one" | `false` | 403 `dm-recipient-not-accepting` | Nothing delivered |
| C requests "Verified users" | `false` | 403 `dm-sender-not-verified` | Nothing delivered |
| C requests "Everyone" | `true` | 201 | C's message requests |

Reading replies:

- `GET /2/dm_conversations/with/:id/dm_events` returned only A's own messages.
- B's reply was not returned, neither with A's token nor with B's own token.
- X showed "this conversation is now end-to-end encrypted" in the app at the moment B replied.
- Sending into the conversation still worked after it became encrypted.

Cost: the console showed 0.07 USD for this run (3 sends, user lookups, one DM event read).
Repeated lookups of the same user on the same day appear to be billed once.

## Decisions

1. **Send with DM v2** (`POST /2/dm_conversations/with/:participant_id/messages`). It works for warm,
   existing and cold-but-open recipients, including recipients on X Chat.
2. **Check `receives_your_dm` before drafting.** It predicted the send result in all five cases. A prospect
   with `false` is marked `blocked` before any LLM cost is spent.
3. **Map the two 403 problem types to user-facing reasons.**
   `dm-recipient-not-accepting` means the person does not accept requests from this account.
   `dm-sender-not-verified` means the person only accepts requests from verified accounts.
4. **Replies are not readable through the API once a conversation is encrypted.** The tool lets the
   user mark a prospect as replied, links to the conversation on X, and states this limitation.
   A time-boxed attempt with the X Chat API (4 hours) is scheduled after the core flow works.
5. **Cold messages land in message requests.** A reply only exists if the person accepts the request.
   This is a platform behaviour to state in the README, not something the tool can change.

## Implication for the outreach account

An unverified sender cannot reach anyone who limits requests to verified accounts. The tool reports how
many prospects in a run were blocked for this reason, so the team can judge whether verification on the
outreach account is worth it.
