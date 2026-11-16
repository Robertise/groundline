# Groundline — write-up (draft)

Nov 15, 2026 · @Robertiz

## What it is

Groundline is a small web app GenAI Fund runs on its own computer. It finds AI and robotics builders on X, shows why each fits with quotes from their own profile and posts, drafts a short DM from that evidence, sends only what a person approved word for word, never messages anyone twice, and tracks what was sent, who replied and what needs attention. Setup: `docker compose up` (or Python 3.11+), an X pay-per-use app, and Bedrock access to Claude Haiku 4.5. Credentials never leave the operator's machine. Repo: [github.com/Robertise/groundline](https://github.com/Robertise/groundline).

## Approach

The model proposes, code decides. Every claim the model makes must quote the person's own text, and code checks the quote is really there before it counts.

1. **Sources:** Pasted handles, search keywords, seed accounts' following lists, or the sender's followers.
2. **Free filters first:** Own account, protected accounts, do-not-contact list, people already messaged, and people X reports cannot receive the sender's DMs (`receives_your_dm`) are removed before any model cost.
3. **Profile screen (8 profiles per model call):** Passes individuals whose text shows AI or robotics work, with a verified quote. Segment fit is reported separately as match / mismatch / unknown. A person is filtered only on a mismatch backed by a verified quote, so people who state no location are kept.
4. **Scoring:** Recent posts are read for the best candidates. The model answers fixed criteria with quotes, and the score is computed in code from the criteria whose quotes survived.
5. **Drafting:** The model writes from verified evidence only and lists each claim with the evidence id it rests on. Code checks length, links, spam or fake-familiarity phrases, unsupported claims and near-duplicates. A second model pass flags tone and unsupported statements. If there is nothing specific to say, it refuses to draft.
6. **Review:** Edit, approve, reject, redraft, or add to do-not-contact. Approval is for the exact text.
7. **Sending:** X DM API v2, one message at a time, 30s apart, 20 a day, behind a lock. Reachability is re-checked right before sending. The intent is written to the database before the request. An uncertain result (timeout, 5xx, crash) becomes **unknown** and is never retried automatically. One outreach row per sender and recipient, enforced by the database.
8. **Tracking:** Queue, Sent, Replies, Needs attention (with the reason and the next step), and a Limits page.

Three modes: `mock` (demo data, fake model, nothing leaves the machine), `dry_run` (real reads, never sends) and `live`. Each keeps its own database file. Stack: Python, FastAPI, server-rendered pages, SQLite, httpx, pytest (211 tests, no network).

## What I learned

A one-day spike with three accounts I control (sender A, warm recipient B, cold recipient C) settled the platform questions before building:

- **DM API v2 works** for followers, existing conversations, and cold accounts that accept requests from everyone (those land in message requests).
- **Two 403 problem types explain every block:** `dm-recipient-not-accepting` and `dm-sender-not-verified`. They are shown to the user in plain words with a next step.
- **`receives_your_dm` predicted the send result in 5 of 5 cases**, so it is used as a free pre-check before any model cost.
- **Replies cannot be read by the API** once X marks a conversation end-to-end encrypted, from either side's token. Replies are therefore marked by hand, with a link to the conversation.

Observations from 5 dry-runs and the final labelled run:

- **An unverified sender cannot reach a large share of the best people:** 37–60% of discovered people per run did not accept DMs. In the final run that included 6/13 confirmed Southeast Asia builders. Verifying the sending account is the single biggest lever on reach.
- **Keyword search mostly returns news and repost accounts**, and a seed's following list inherits the seed's geography: a seed based in Germany produced 42 people, none in Southeast Asia. Curated handles and seeds who are themselves in the region work best.
- **Active Southeast Asia AI builders on X are few and skew to Singapore academics:** Of 34 hand-picked accounts, 12 were confirmed in the region and 9 of those were in Singapore.
- **Profile lookups dominate X cost**, and the app's estimate runs about 8–9% above the X console. Post-read estimates matched the console within 2%, the gap appears to come from repeated same-day lookups of the same person, which X seems to bill once.

## Results

Thresholds were written down before each measurement. Where a sample came out smaller than planned, I report raw counts instead of a pass or fail.

| What we measured | Target, set in advance for planning | Result | Verdict |
| --- | --- | --- | --- |
| Duplicate messages under injected failures | 0 in 200 seeded batches | 0 | Pass |
| Live send to my 2 test accounts | 1 message each | 1 each, 30.7s apart. No double-messaging | Pass |
| Precision of people kept | ≥ 0.80, on ≥ 20 people kept | 8/8 fit (on strict rule: 6/8), on 9 kept | Too few to judge |
| Fit people wrongly set aside | report | 1/9 | – |
| Non-fit people correctly set aside | report | 10/10 | – |
| Invented claims in drafts | 0 in ≥ 30 drafts, ≤ 2 need rewording | 0 in 9 (two readers), 1 slight overstatement | Too few to judge |
| Setup time from a fresh clone | mock < 5 min, live < 15 min | mock: 2 min, live: 4 min | – |

How to read it: "kept" means scored 60 or more. Precision counts 8 people, 1 labelled unsure was left out, and "strict" counts only people confirmed to be in Southeast Asia. The one fit person set aside was a community lead whose bio shows no AI work. The 10 correctly set aside: 4 outside the region (location quoted), 5 not doing AI work, 1 whose quote could not be verified. Rows marked "report" had no threshold. "too few to judge" means the sample was smaller than the target required, so only counts are given.

Targeting was measured on a sample of 35 accounts I labelled by hand on two questions: is this an individual doing AI or robotics work, and are they in Southeast Asia (yes, no, unknown). "Fit" = AI builder and not clearly outside the region, matching the product rule that missing location is not a reason to exclude. 13/35 could not receive DMs from the unverified sender and were not scored. The sample is small and Singapore-heavy, so these numbers flatter the tool compared with open-ended discovery.

## Trade-offs

| Choice | Why | Cost of the choice |
| --- | --- | --- |
| Self-hosted source + Docker, not a hosted service | GenAI's X token and AWS keys stay on GenAI's machine | Run a container instead of opening a link |
| Replies marked by hand | The API returns nothing from encrypted conversations | One click per reply. No automatic reply classification |
| Human approval of exact text, no follow-ups | Personal outreach at this scale needs a person in the loop | Slower than auto-send |
| Filter on location only with a verified mismatch | Many builders state no location, making it mandatory would drop them | Some people outside the region get through as "unknown" |
| Score computed in code from verified criteria | Model scores drift; quotes can be checked | Less nuance than a free-form model judgment |
| One model (Claude Haiku 4.5 on Bedrock) behind one adapter | Cheap, fast, and its terms do not allow training on submitted data | A second provider is future work |
| SQLite, server-rendered pages, no agent framework or vector store | One operator, hundreds of people: nothing here needs more | Not multi-user |

Rejected options, with the reason: scraped or third-party X data and browser automation (against X's rules and the brief); enrichment from LinkedIn or GitHub (X's off-platform matching rule); fine-tuning on X posts (X's developer terms forbid it); Google AI Studio's free tier (its terms allow training on submitted content, which conflicts with X's terms); model-written replies (a reply is where the conversation becomes personal, so a person writes it).

## Costs

Paid tools: X API pay-per-use and Amazon Bedrock. Nothing else is paid. Building and testing used about 2.02 USD of X credit and about 0.25 USD of Bedrock.

| Item | Cost |
| --- | --- |
| Final labelled run: 35 handles in, 9 people scored and drafted | 0.65 USD discovery (0.59 X, 0.06 model) + 0.05 USD drafting |
| Per drafted message, counting everyone filtered on the way | about 0.08 USD |
| Per DM sent | 0.010 USD (estimate, not separable on the console yet) |
| Model share | under 10% of the total. X reads are the cost |

At the default cap of 20 messages a day, a full month of outreach would cost roughly 50 USD with the final run's funnel. Each campaign shows a maximum cost estimate before it runs and has a budget that stops a run; the X console's own spending limit is the backstop.

## How AI tools were used and checked

Claude helped with strategy, design and verification, and wrote an explicit implementation plan for each stage (exact files, signatures, SQL, test names, acceptance checks, proposed commits). The coding agent in Antigravity (Gemini Flash) implemented each plan. I reviewed every diff and made every commit myself, one logical change per commit.

Nothing was accepted on the agent's own report. Each stage was checked by:

1. Reading the actual diff against the plan, including what it should not have touched.
2. Running the full test suite in a clean virtual environment.
3. Mutation checks: deliberately breaking the code a new test is meant to protect and confirming the test fails. About 25 such bugs were injected; 4 went undetected at first, 3 of those tests were rewritten, and 1 gap was accepted as low risk.
4. Reading the SQLite database and the X console after real runs, rather than the screen.

4 times the AI-produced work was wrong:

- **Silent loss of people:** A change I had planned added 2 fields to each screening answer. 20 profiles per call then exceeded the model's 2,000-token output limit, and 20/22 people were skipped while the run reported success. Caught by comparing the number of people scored with the token counts in the cost ledger. Fixed: truncation is now an error, batches are 8 and split in half when cut off, and unanswered people are listed as errors.
- **A hidden filter:** The screening prompt received the target segment without being told how to use it, and the model quietly failed AI researchers for living outside the region, labelled as "not AI work" (0 of 42 people passed in one run). Caught by reading the model's stored reasons. Fixed with a separate, quote-verified segment field.
- **Tests that could not fail:** Agent-written tests included `screened < screened + 1`, an expected value read from the code under test, and a check for text that was always on the page. Caught by mutation checks.
- **A prompt fix that did not work:** The model treats a flag emoji in a bio as a location match. One prompt revision was allowed by the rule I set in advance. It did not change the result, so this is reported as a known limitation instead of tuned further.

## Limitations and what comes next

Not finished or not possible today:

- Replies are marked by hand, the app cannot see them.
- A flag emoji or nationality in a bio can be read as a location match (the quote is shown, so the reviewer can judge).
- People pasted by handle with an empty bio are filtered: there is nothing to verify.
- The second drafting check sometimes flags a question as an unsupported claim.
- One sending account, one operator, cost figures estimated from list prices.

Next, in order of value:

1. **Verify the sending account**, then re-measure reach. This alone could roughly double the reachable pool.
2. **Time-boxed trial of the X Chat API** to read replies automatically.
3. **Region-specific seed lists** (Southeast Asia labs, communities, founders) so discovery does not depend on hand-picked handles.
4. **A larger labelled sample** (100+ people across countries and roles) to measure targeting properly, and to fix the location and question-flag cases against it.
5. **A freshness check before sending**, so a message approved days earlier is not sent on stale evidence.
6. **A second model provider** behind the existing adapter, and a shared, hosted version if GenAI wants several people operating it.
