# Groundline

Evidence-grounded X outreach for AI and robotics builders.

Groundline finds relevant people on X, shows why each one fits with quotes from their own profile and posts,
drafts a short DM from that evidence, sends only what a person has approved, never messages the same person
twice, and shows what was sent, who replied and what needs attention.

It is a small web app you run on your own computer. Your X and AWS credentials stay on your machine.

## What you need

| For | You need |
| --- | --- |
| Trying it (mock mode) | Docker, or Python 3.11+ (tested with 3.12). No accounts, no keys. |
| Reading real X data (dry-run) | An X developer app on the pay-per-use plan with credit, and the X account you will send from. |
| Scoring and drafting with a real model | An AWS account with Amazon Bedrock access to Claude Haiku 4.5. |
| Sending (live) | The same as dry-run. |

## Try it in two minutes (mock mode)

Mock mode uses 25 invented demo accounts and a built-in fake model. Nothing leaves your computer.

With Docker:

```
git clone https://github.com/Robertise/groundline.git
cd groundline
docker compose up --build
```

Without Docker:

```
git clone https://github.com/Robertise/groundline.git
cd groundline
python -m venv .venv
.venv\Scripts\activate          (Windows)   or   source .venv/bin/activate   (macOS, Linux)
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 (use `127.0.0.1`, not `localhost`). Then:

1. **Campaigns → + New Campaign.** Enter a name, the segment
   `AI and robotics founders and builders in Southeast Asia`, the keywords `robot, LLM, agent` and the seed
   handle `demo_seed_investor`. Leave the rest and save.
2. **Run discovery.** People appear with a score, the reason they fit, and the quotes behind it. Filtered
   people show why they were filtered.
3. **Draft messages**, then **Review drafts.** Each draft lists the claims it makes and the evidence for each.
   Edit if you like, **Save edit**, then **Approve**.
4. **Outreach → Send approved.** The messages move to **Sent**. Press it again: nothing is sent twice.
5. On **Sent**, type a note and press **Mark replied**. Look at **Needs attention** and **Limits**.

## Set up for real use (about ten minutes)

### 1. Create the settings file

Copy `.env.example` to `.env`. Never commit `.env`.

Create an encryption key for the stored X token and put it in `GROUNDLINE_SECRET_KEY`:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 2. X developer app

1. At https://developer.x.com create an app on the pay-per-use plan and add credit.
2. In the app's user authentication settings, turn on OAuth 2.0 with app permissions
   **Read and write and Direct message**, app type **Web App**, and this exact callback URL:
   `http://127.0.0.1:8000/callback`
3. Copy the OAuth 2.0 Client ID and Client Secret into `GROUNDLINE_X_CLIENT_ID` and
   `GROUNDLINE_X_CLIENT_SECRET`.
4. Set a monthly spending limit in the X developer console.

The app asks for these scopes: `tweet.read users.read follows.read dm.read dm.write offline.access`.

### 3. Amazon Bedrock

1. In the AWS console, in region `ap-southeast-1`, make sure your account can use Claude Haiku 4.5 in Bedrock.
2. Use credentials that are allowed to call `bedrock:InvokeModel`. Either run `aws configure`, or fill
   `GROUNDLINE_AWS_ACCESS_KEY_ID` and `GROUNDLINE_AWS_SECRET_ACCESS_KEY` in `.env`.

To use another region or model, change `GROUNDLINE_AWS_REGION` and `GROUNDLINE_BEDROCK_MODEL_ID`.

### 4. Start in dry-run mode and connect X

Set `GROUNDLINE_MODE=dry_run` in `.env`, start the app as above, open http://127.0.0.1:8000 and press
**Connect X**. Sign in with the account you will send from. The header then shows "Connected as @you".

Dry-run reads real profiles and posts, scores and drafts with the real model, and never sends. Use it to check
targeting and drafts before you go live.

### 5. Go live

Set `GROUNDLINE_MODE=live`, restart, and press **Connect X** again (each mode keeps its own database file, so
the live database has no token yet). The header shows a red LIVE badge. From here, **Send approved** sends real
DMs.

To rehearse safely, set `GROUNDLINE_LIVE_ALLOWED_HANDLES` to a comma-separated list of test accounts. While it
is set, nobody else can be messaged.

## The three modes

| Mode | Reads X | Model | Sends | Database file |
| --- | --- | --- | --- | --- |
| `mock` | no (demo data) | built-in fake | to an in-memory stand-in only | `data/groundline.db` |
| `dry_run` | yes | Bedrock | never | `data/groundline-dryrun.db` |
| `live` | yes | Bedrock | yes, after approval | `data/groundline-live.db` |

## How a campaign works

1. **Sources.** Paste handles, give search keywords, give seed accounts (the app reads who they follow), or
   include your own followers. Any mix.
2. **Free checks first.** Your own account, protected accounts, people on your do-not-contact list, people
   already messaged, and people X says cannot receive your DMs are filtered before any model cost.
3. **Profile screen.** The model reads bio, location and one post per person, and passes only individuals whose
   text shows AI or robotics work. Every pass must quote the profile; the app checks the quote is really there.
   A person whose stated location is clearly outside your segment is filtered, with the quote shown.
4. **Scoring.** For the best candidates the app reads recent posts. The model answers fixed questions with
   quotes; the score is computed in code from the answers that kept a verified quote.
5. **Drafting.** The model writes a short DM from verified evidence only and lists each claim with the evidence
   it rests on. Code checks the draft (length, links, spam or fake-familiarity phrases, claims without evidence,
   near-duplicates of your other drafts). A second model pass flags unsupported statements, a tone that
   pretends to know the person, a missing easy way to say no, and sensitive personal traits. If there is nothing specific
   to say, the app says so instead of writing a generic message.
6. **Review.** You edit, approve, reject, or add the person to the do-not-contact list. Approval is for the
   exact text.
7. **Sending.** One message at a time, about 30 seconds apart, at most 20 a day (both configurable). The app
   re-checks that the person can receive your DM immediately before sending.
8. **Tracking.** Sent, Replies and Needs attention. See the limits below for replies.

## Why it does not double-message

- The database allows one outreach row per sender and recipient, across all campaigns.
- The intent to send is recorded before the request goes to X.
- If the result is uncertain (timeout, server error, the app stopped mid-send), the message is marked
  **unknown** and is never retried automatically. You check the conversation on X and choose "It was delivered"
  or "It was not delivered".
- A test runs 200 seeded batches with injected failures and crashes and asserts that no recipient receives two
  messages.

## Limits you should know

- **Replies are marked by hand.** X does not return messages from encrypted conversations to apps, so the app
  cannot see replies. Each sent row links to the conversation on X.
- **An unverified sending account cannot reach everyone.** In test runs, roughly half of the discovered people
  did not accept DMs from an unverified account. The app filters them before any model cost and shows the
  count. Verifying the sending account widens reach.
- **Cold DMs land in message requests.** The person sees them only if they open their requests.
- **Location is taken from what the profile says.** People who state no location are kept, without the
  segment points. A flag emoji in a bio can still be read as a location match; the quote is shown so you can
  judge.
- **People pasted by handle with an empty bio are filtered**, because there is nothing to verify.
- **One sending account**, one person operating it. No follow-ups, no auto-follow or likes, no scraping, no
  data from outside X.
- **Cost figures in the app are estimates** and run slightly above the X console (about 8–9% in testing).
  The X console is the source of truth.

The **Limits** page in the app lists what the tool will not do.

## Costs

All paid by you, directly to X and AWS. The campaign page shows a maximum cost estimate before each run, and
each campaign has a budget that stops a run.

| Item | Estimate used by the app |
| --- | --- |
| Profile looked up on X | 0.010 USD |
| Post read on X | 0.005 USD |
| Person read through a seed's following list | 0.010 USD |
| DM sent | 0.010 USD (estimate) |
| Model (Claude Haiku 4.5 on Bedrock) | about 0.01–0.02 USD per person scored and drafted |

A person who is scored costs about 0.06 USD in X reads (one lookup plus up to ten posts). Building and testing
this tool used about 1.5 USD of X credit and well under 1 USD of Bedrock usage in total.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| A red banner about missing settings | Fill the named values in `.env` and restart. |
| "Connect X" does not come back to the app | The callback URL in the X app must be exactly `http://127.0.0.1:8000/callback`, and you must open the app at `127.0.0.1`, not `localhost`. |
| The header shows no connected account after changing mode | Each mode has its own database. Press **Connect X** again. |
| A run stops with `budget_reached` | Raise the campaign budget or narrow the sources. |
| A run stops with `rate_limited` | Wait for the X rate-limit window, then run again; finished people are kept. |
| A run stops with `auth_error`, or a message fails with "X rejected the app's access" | Press **Connect X** again. |
| The model call fails | Check Bedrock model access in the chosen region and the `bedrock:InvokeModel` permission. |
| "Sending is already in progress" | A batch is running. Refresh the Outreach page. |
| A message is in Needs attention as **unknown** | Open the conversation on X, check, and choose delivered or not delivered. |

`GET /health` returns the mode and whether interrupted sends were recovered at startup.

## Development

```
pip install -r requirements.txt
pytest -q
```

The tests use no network and no keys.

## Licence

MIT. See `LICENSE`.
