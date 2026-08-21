# Adjourn — demo runbook

**Aug 21, 2026 · one standup — the PR review is inside it — and a board that fills itself.**

The claim: *the meeting is the to-do*. You hold a normal standup inside Adjourn,
you stop the recording, and the follow-through is already done — issues
commented, tickets moved, a draft PR opened, a Slack message sent, time held on
the calendar, a recap written — before anyone has switched windows.

The architecture claim underneath it: **a model extracts what was said; a
deterministic table decides what happens.** No model anywhere in this system
decides *whether* to act. That is the whole reason it can be trusted to act
without asking.

And the claim that actually wins the room: **most of what is said produces
nothing, on purpose.** Half the regression corpus in `adjourn/fixtures/regression/`
exists to fire zero actions. Section 6 is how you show that.

---

## 0. Two steps that have not been done yet

Read this first. As of this writing, `sharique2004/adjourn` **has no commits**,
which means the roadmap issues and the prop pull request the demo points at do
not exist yet. Everything else is ready.

```bash
cd /path/to/adjourn
PY=python

# 1. build the curated public tree and read the push commands it prints
$PY -m adjourn.tools.publish_tree --out /tmp/adjourn-push
#    …then run those commands. It never pushes for you.

# 2. seed the demo world inside the repo Adjourn now manages
$PY -m adjourn.tools.seed_demo_world

# 3. verify
gh issue list -R sharique2004/adjourn        # #1..#5
gh pr view 6 -R sharique2004/adjourn         # the prop, open, one commit

# 4. re-seed memory now that the issues exist
$PY -m adjourn.reset_demo --reseed
```

**Step 4 is not optional and it is easy to skip.** `seed_memory` records the
repository's open issues as `Issue` nodes, and against an empty repo it records
none — the log says so plainly: `0 issue(s) recorded from sharique2004/adjourn`.
The conflict beat still works either way, because the topic→issue index is built
from the numbers *spoken* in the prior standup rather than from GitHub. But the
graph is thinner than it should be, and the Connections tab will tell on you.
After step 2, that line should read `5 issue(s) recorded`.

`seed_demo_world` **refuses on an empty repository** — there is nothing to
branch from — so it will tell you if you have the order wrong. Run it with
`--dry-run` any time; it validates every anchor and writes nothing.

It files five issues and then the prop, so the prop lands at **#6**. If GitHub
assigns a different number the seeder rewrites the spoken line in
`pr-review-beat.jsonl`, the ground truth beside it, and `PR_REVIEW_PROP.md` to
match, and prints what it changed. **Read that output** — the number you say out
loud in §7 comes from there.

**Issue order is load-bearing and the seeder enforces it.** `prior-standup.jsonl`
says *"issue two"* about the cache layer and *"issue one"* about the auth
migration, and memory builds its topic→issue index from those spoken words
rather than from GitHub. Seed the repo in a different order and nothing errors:
every card still fires, onto issues whose titles have nothing to do with them.

| Issue | Title | Mirrors |
|---|---|---|
| #1 | Auth migration: short-lived service tokens | — (GitHub only) |
| #2 | Cache layer for meeting graph queries | SHA-6 |
| #3 | Migrate transcript ingestion to the streaming adapter | SHA-5 |
| #4 | Retry logic for executor failures | SHA-7 |
| #5 | Rate-limit the public board API | SHA-8 |
| #6 | **[demo-prop] Add join button to landing page** — the PR you review in §7 | — |

---

## 1. Pre-demo checklist

Work top to bottom. About four minutes. The last two steps are the ones that
actually break.

```bash
cd /path/to/adjourn
PY=python                          # a virtualenv with requirements.txt installed
```

| # | Check | Command | What you want to see |
|---|-------|---------|----------------------|
| 1 | MeetingScribe engine up | `curl -s 127.0.0.1:5005/api/record/status` | `{"recording": false, …}` |
| 2 | Live captions enabled | `curl -s '127.0.0.1:5005/api/live?since=0' \| head -c 60` | `"enabled": true` |
| 3 | FalkorDB up | `docker exec falkordb-test redis-cli PING` | `PONG` (if not: SQLite takes over, see §9) |
| 4 | The whole suite is green | `$PY -m adjourn.tests.run_all` | `ALL 16 SUITES PASSED` in about 70s |
| 5 | `gh` authenticated | `gh auth status` | logged in as `sharique2004` |
| 6 | Clean slate | `$PY -m adjourn.reset_demo --reseed` | see below |
| 7 | Board up | `$PY -m adjourn.board_server` | serving on `http://127.0.0.1:5117` |
| 8 | Everything connected | open **`http://127.0.0.1:5117/connections`** | read it, top to bottom — this is step 4's replacement for squinting at a terminal |
| 9 | Orchestrator watching | `$PY -m adjourn.orchestrator --watch` | `engine … reachable=True` |

**Step 8 is the new pre-flight.** The Connections tab is a read-only page that
answers, per executor, whether it is LIVE or SIM and *why* — plus engine
reachability, memory backend and node counts, the cloud probe, secret
**presence** (never values), and every target: repo, allowlist, Linear team,
journal path, recap directory, board bind. If something is going to be
simulated tonight, this page told you before the founders sat down.

### Dress the board (optional, 1 second)

A board with nothing on it is an honest cold open, and it is also a weak first
frame if the room walks in early. One command puts the last rehearsal's twelve
receipts on Follow-through — all five demo kinds, the PR suggestion included —
without running extraction, without touching the network, and without a server
restart:

```bash
$PY -m adjourn.tests.dress_journal      # then reload http://127.0.0.1:5117/
```

It writes `executions.jsonl` and nothing else, and every card carries the badge
that is true of it. **Undo it before you present** with step 6 (`reset_demo
--reseed`), which archives that journal — otherwise the live `--replay` lands on
top of a board that already looks finished and the fill is invisible.
`--into DIR` writes somewhere harmless instead, for a rehearsal you do not want
on the real journal.

### Reading what `--reseed` prints

The conflict beat — the best moment in the demo — only works if last week's
standup is in memory for tonight's to contradict. `--reseed` runs the *prior*
meeting through the real extractor and ingests it into **both** backends. It
takes about a minute.

**Three resolution lines print per backend.** You need:

```
[seed] falkor: 'cache layer' -> issue 2      ← must be 2
[seed] falkor: 'auth migration' -> issue 1   ← must be 1
[seed] falkor: 'streaming adapter' -> issue None  [ok — this one is a Linear
                                       ticket (SHA-5), not a GitHub issue]
```

`-> issue None` on the streaming adapter is **correct** and the seed says so
itself now. Re-run `--reseed` only if one of the first two says `None`.

Read the topic word in the `known topics` line: the seed is extracted by a
model, so it names that first topic `cache layer` on most runs and `caching` on
some. **That same word titles both calendar cards** — if it picked `caching`,
your holds read *"Ship: caching"* and *"Review: caching"* and §8's quoted titles
are off by a word. Everything still fires; only your patter needs adjusting.

### Zoom

At 100% on the laptop nothing on the board reads from a couch — the mode badge
subtends about four arcminutes from 1.5m and the floor for legibility is eight
to ten. Put the browser at **175–200%**, or mirror to a large display.

That used to be mutually exclusive with seeing any cards: at 175% on a 1512px
window the header and the pipeline panel ate the entire first screen. **The
pipeline is now an always-on rail down the right-hand side** rather than a block
above the cards, so the cards keep the first screen and the stages stay visible
the whole time. Measured after the change, on the dress-rehearsal journal:

| Viewport | Star card top | Cards above the fold |
|---|---|---|
| 1512 × 900 (100%) | 325px | 2 fully, 3 starting |
| 864 × 514 (175%) | 269px | 1 fully, 1 starting |

Rehearse at the zoom you will actually present at.

### Windows, left to right

1. **Adjourn** — `http://127.0.0.1:5117` — the whole demo lives here now. Five
   tabs: **Meetings · Live · Follow-through · Ledger · Connections**.
2. GitHub issue #2 — `https://github.com/sharique2004/adjourn/issues/2`
3. The prop PR — `https://github.com/sharique2004/adjourn/pull/6`
4. The Linear SHA board — SHA-5 visible in **In Progress**
5. Your phone, on the Slack workspace, `#all-test`

Verify the starting state out loud before you begin, so the change is visible:
issue #2 has **no** comments, the prop PR has **no** reviews, SHA-5 is **In
Progress**, Follow-through is **empty**.

---

## 2. Mode: decide this before you start

| Setting | What happens | When to use it |
|---|---|---|
| *(nothing set)* | Everything live: GitHub, Linear, Slack. Email stays sim — no Gmail app password is provisioned. | The real demo, if you are comfortable. |
| `ADJOURN_SIM=1` + `ADJOURN_LIVE_KINDS=github_update,pull_request_stub,pr_review_suggestion` | GitHub is real; Linear, Slack, calendar, email are simulated. | **Recommended.** The live transports write somewhere public and reversible; every other card renders the exact payload it would have sent. |
| `ADJOURN_SIM=1` **with `ADJOURN_LIVE_KINDS` unset** | Nothing leaves the machine. Every card badged SIM. | The plane, a bad network, or a room where you would rather not send anything. |
| `--sim-all` | Same, but it *also clears* `ADJOURN_LIVE_KINDS`. | **The panic button.** Use this, not `--sim`, when you want a guarantee. |

⚠️ `--sim` alone does **not** mean "nothing leaves this machine" if
`ADJOURN_LIVE_KINDS` is set in `adjourn/.env` or exported in your shell.
`ADJOURN_LIVE_KINDS` narrows `ADJOURN_SIM` no matter how `ADJOURN_SIM` got set,
so GitHub would still be live. The orchestrator prints which one you got:

```
[orchestrator] --sim: ADJOURN_SIM=1 — sim EXCEPT github_update, pull_request_stub,
               which ADJOURN_LIVE_KINDS keeps LIVE (use --sim-all to force everything to sim)
```

Read that line. If it does not say *nothing will leave this machine*, it will.
`ADJOURN_LIVE_KINDS` can only ever *narrow* `ADJOURN_SIM=1` — a kind listed
there still needs its secret, still prints its decision, and still wears
whichever badge is true. It cannot turn a missing credential into a live send.

**As shipped, `adjourn/.env` has `ADJOURN_SIM=0` and an empty
`ADJOURN_LIVE_KINDS` — everything is live.** That is the right setting for the
demo and the wrong one for an idle laptop. The Connections tab is where you
confirm it.

To run the recommended mode:

```bash
ADJOURN_SIM=1 ADJOURN_LIVE_KINDS=github_update,pull_request_stub,pr_review_suggestion \
  python -m adjourn.orchestrator --watch
```

---

## 3. The shape of the demo

One window. Five tabs. You move left to right across them and the product tells
its own story in that order.

| Act | Tab | What happens | ~time |
|---|---|---|---|
| 1 | **Meetings** | Open on the library — 54 real recordings, 12.6 hours. "This is a meeting recorder. That part is not the product." | 15s |
| 2 | **Live** | Press **Start recording** *in Adjourn*. Captions stream in as you talk. Hold the standup — the PR review is its last forty seconds. | 130s |
| 3 | **Live → stop** | Press **Stop**. Nothing else is clicked, all night. | 1s |
| 4 | **Follow-through** | 30–50 seconds of silence, then every card lands at once. This is §8. | 60s |
| 5 | **Follow-through** | Undo a card. Watch the external state come back. | 20s |
| 6 | **Follow-through** | The recap link, same origin, one click. Close on it. | 20s |

**ONE MEETING. There is no second one.** The PR-review beat (§7) used to be a
separate 40-second tape you ran afterwards, and it is now the tail of the
standup itself — lines 28–36 of §4. That is why one bare `--replay` is the
entire pitch: the same tape produces the Linear ticket, the SHA-5 move, the
Slack draft, the GitHub decision comment **and** the inline suggestion on
PR #6. Do not split it back out; the beat that shows Adjourn reading code is
worth more inside one meeting than as an encore nobody has time for.

Open on **Meetings**, not on the board. The library is a real artifact — 54
recordings with durations, speakers, warnings and summaries — and starting
there makes the point that the follow-through is a layer on top of something
that already works, rather than the whole trick.

---

## 4. The 90-second script

Two people. **YOU** is Sharique (mic track). **THEM** is the other person, on
speaker or in the room (system track). Say it like a standup, not like a script:
the filler and the interruptions are what make it a real meeting, and Adjourn is
built to ignore them.

**The live-mic variant — exactly what has to be running.** Two processes, two
terminals, no third window. The board serves the tabs; the orchestrator is what
notices Stop:

```bash
cd /path/to/adjourn
PY=python

# terminal 1 — the five tabs. Leave it up. http://127.0.0.1:5117
$PY -m adjourn.board_server

# terminal 2 — the watcher. THIS is what makes Stop do anything.
ADJOURN_SIM=1 ADJOURN_LIVE_KINDS=github_update,pull_request_stub,pr_review_suggestion \
  $PY -m adjourn.orchestrator --watch
```

`--watch` starts MeetingScribe if it is not already up, primes against the jobs
that already exist (so the back catalogue does not fire), and then holds two
polls: `/api/record/status` at 2 Hz and `/api/status` at 1 Hz. The chain Stop
sets off is: the Live tab proxies `POST /api/record/stop` to the engine verbatim
→ `recording` flips `true` → `false` → the fast poll sees that edge and **forces**
an immediate `/api/status` pull instead of waiting out the slow tick → the new
key under `jobs` is the stop edge *and* the meeting id in one observation →
`handle_meeting_stopped` runs the fast pass, so cards land while the transcript
is still being written → when that job flips to `done`, the final pass reconciles
against the full transcript. That whole path is `orchestrator.handle_meeting_event`
and it is asserted end to end in `test_lane_a_spine` under *"the live handoff"*.

**If terminal 2 is not running, Stop does nothing** and the board sits there
looking broken. That is the single most likely way to lose the demo; check §1
step 9 before you walk on.

Press **Start recording** on the Live tab. Then:

| # | Who | Line | What it becomes |
|---|-----|------|-----------------|
| 1 | THEM | "Okay, we're live. Is the recording thing running?" | *nothing* — meta-talk about the meeting |
| 2 | YOU | "It's running. Ignore it, it does its thing." | *nothing* |
| 3 | THEM | "Right. So, uh, cache layer. I have bad news and good news." | *nothing* — setup, not a decision |
| 4 | YOU | "Bad news first, obviously." | *nothing* |
| 5 | **THEM** | **"Bad news is Redis is overkill. I profiled the ingestion path last night and the hot set is sixty megabytes. So we're dropping Redis on issue two and going with an in-process LRU instead."** | ⭐ **GitHub comment on #2** — the what-changed card, *Before: Redis · After: in-process LRU*, because last week's standup said Redis |
| 6 | YOU | "Sixty megs. That fits in a browser tab." | *nothing* — a joke |
| 7 | THEM | "Right? Less code, and one less service on the pager." | *nothing* |
| 8 | YOU | "Okay. And who's carrying it? Sam's buried in the adapter work." | *nothing* — a question someone answers is setup |
| 9 | **THEM** | **"Priya's taking the cache work off Sam. She's the one who found the sixty megabytes, so she should finish it."** | **GitHub comment on #2** — reassignment |
| 10 | YOU | "Fine. Ship date still the twenty-second?" | *nothing* |
| 11 | **THEM** | **"Push it to the twenty-fifth. She needs the weekend to rip the Redis client out."** | **Calendar hold — Tue 25 Aug** |
| 12 | YOU | "Works. Hold on — webhook signature verification. Did anyone ever actually file that?" | *nothing* |
| 13 | **THEM** | **"No. We keep talking about it and it lives nowhere. We need a ticket for webhook signature verification on the ingestion endpoint."** | **Linear ticket created** |
| 14 | YOU | "Yeah, that one's been verbal for three weeks now." | *nothing* |
| 15 | **THEM** | **"Speaking of — SHA-5, the streaming adapter. I'm about eighty percent done with it, should be in review tomorrow."** | ⭐ **Linear SHA-5 moves In Progress → In Review** — see §8 for why |
| 16 | YOU | "Nice. That unblocks the gateway work." | *nothing* |
| 17 | **THEM** | **"And I'll open a PR for the config loader tonight. It's draft-ready, I just want eyes on the interface before I go further."** | ⭐ **Draft PR opened on `sharique2004/adjourn`** |
| 18 | YOU | "Tonight tonight, or tomorrow-morning tonight?" | *nothing* |
| 19 | THEM | "Tonight tonight." | *nothing* — and **no** calendar hold, because it is not a deadline statement |
| 20 | YOU | "Okay. Auth migration, quickly — issue one, anything I need to know?" | *nothing* |
| 21 | **THEM** | **"It's been running clean in dev all week. Nothing to report, which is the report."** | **GitHub comment on #1** — resolved through memory, since "issue one" was said in the *previous* line |
| 22 | YOU | "Do we still need the rate-limit ticket open, or is SHA-5 the same piece of work?" | *nothing* — an open question, nobody answered it |
| 23 | THEM | "Uh. Good question. I'll look after this." | *nothing* |
| 24 | **YOU** | **"Alright. I'll Slack the channel the summary once we're done here."** | ⭐ **Slack draft** — Ready to send; edit, then Send |
| 25 | **THEM** | **"And I'll email Alex the deck after this. He's asked for it twice now and I keep forgetting."** | **Email draft** — Ready to send; edit, then Send |
| 26 | **YOU** | **"Perfect. Let's review Friday, both of us, and see where the cache actually landed."** | **Calendar hold — "Review: cache layer"**. Today *is* Friday, so a bare "Friday" on a review beat rolls to the 28th and the log says so out loud. (A *deadline* said as "by Friday" still means today — different reading, on purpose.) |
| 27 | THEM | "Friday works. Okay, I'm going to go eat something." | *nothing* |
| 28 | YOU | "Two more things and then I'll let you go eat." | *nothing* — the hinge into §7, still the same meeting |
| 29 | THEM | "I'm listening. Go." | *nothing* |
| 30 | YOU | "Did you look at the beta signup page? Priya pushed it up this afternoon, the join the beta thing." | *nothing* — a question |
| 31 | THEM | "I skimmed it on my phone in the elevator. Copy's good. Shorter than I expected, which is a compliment." | *nothing* |
| 32 | **YOU** | **"Yeah, the copy's fine. But on Priya's join button PR — pull six — that green is wrong. It should be our slate blue, six B seven F nine nine."** | ⭐ **Inline review suggestion on PR #6** — on the diff, `#2ecc71` → `#6b7f99`. Not a commit, not a push. See §7 |
| 33 | THEM | "Six B seven F nine nine. Right, the one out of the deck. I always have to look that up." | *nothing* |
| 34 | YOU | "It's the only green anywhere on the site. It doesn't read as a brand colour, it reads as a bug someone hasn't noticed yet." | *nothing* — reasoning about a change already asked for |
| 35 | THEM | "No, you're right. It's one line in the stylesheet, she can turn that around before Friday without breaking a sweat." | *nothing* — and **no** calendar hold. "Can" is not "will", and she was not in the room. See §7 |
| 36 | YOU | "That's all I had. Go eat something." | *nothing* |

**Then press Stop.** That is the whole trigger. Nobody clicks anything else in
Adjourn, and there is no second meeting.

Eleven beats out of thirty-six lines produce an action. The other twenty-five
are the demo: this is a system that mostly does nothing, on purpose.

Do **not** promise a card count out loud. Extraction is a model. Measured across
six identical replays tonight, the demo transcript produced **11–13 statements
and 8–10 acting cards**. The ten *beats* are stable; the arithmetic around them
is not. Say "about ten".

---

## 5. Speaker names

MeetingScribe labels the microphone track `You` and the system track `Them`.
Both are correct on the recorder's own screen and wrong everywhere else — a
Slack message that opens *"You in the meeting:"* means nothing to whoever reads
it, and an email card reading *"Them will email Alex the deck"* looks like a bug
on a projector.

Both are now mapped once, in extraction, so the Slack body, the Linear
description, the GitHub blockquote and the recap ledger all get it for free:

```
ADJOURN_SPEAKER_NAME=Sharique   # the mic track. Default: Sharique
ADJOURN_GUEST_NAME=Guest        # the system track. Default: Guest
                                # (ADJOURN_OTHER_SPEAKER_NAME is the old spelling
                                #  and is still read when the new one is unset)
```

The far end is a **description, not a name**, because Adjourn genuinely does not
know who spoke — and inventing a "Priya" who was never identified is exactly the
kind of confident wrongness this product is arguing against. When diarisation
*does* name someone, that name arrives already resolved and passes straight
through untouched.

---

## 6. The restraint act — the part that wins the room

If a founder asks *"what stops it filing junk?"*, do not answer with a
paragraph. Open a terminal and run the corpus:

```bash
ADJOURN_SIM=1 python -m adjourn.tools.replay_check
```

Nine transcripts through the real extractor and the real planner, writing
nothing anywhere. **Five of them are supposed to produce zero actions**, and
four of those were written by an auditor whose job was to break this and who
succeeded on the first attempt:

| Scenario | What is in it | Correct answer |
|---|---|---|
| `fp-hypothetical` | *"And don't email the client yet. I mean it."* · *"We could file a ticket for this but honestly let's not."* | nothing |
| `fp-reported` | 19 segments of *"Alex said he'd send the deck"* — nobody in the room commits to anything | nothing |
| `fp-social` | dinner. *"I promise you'll love this place"* is not a commitment | nothing |
| `fp-meta` | a meeting **about** tickets and PRs and emails, which must produce none of them | nothing |
| `no-actions` | small talk — **and the recap still fires**, so restraint leaves evidence | nothing but a recap |
| `hallway-sync` | two real beats buried in chat | a comment and a ticket |
| `design-review` | the full spread | six actions |
| `living-room-standup` | **the demo meeting** — the whole pitch, PR beat included | all eight action kinds |
| `pr-review-beat` | the PR beat on its own, kept as a regression tape — **not** something you run on stage | one review, and *nothing else* |

Each of those first four once produced live public artifacts: a prohibition
published to an issue as a decision, a Linear ticket filed in the name of
someone who never spoke, and a real branch with a real draft PR. They are kept
because that is what a regression fixture is.

There are two defences and they are independent. The prompt names four shapes of
sentence that look actionable and are not — negations, counterfactuals, reported
speech, and *"she can turn that around before Friday"* estimates of an absent
person's work. Underneath it, a deterministic gate reads the **verbatim quote**
(never the model's cleaned-up claim, which is where modality dies) and holds the
statement before the routing table ever sees it.

The scoping is the whole trick, and each of these is a test:

- *"we are **not** using Redis for the ingestion cache"* → **not** held. A negated
  *tool* is a decision; a negated *action* is restraint.
- *"Can we get a ticket cut…? I **don't** want it to just live in this
  conversation."* → **not** held. The don't-sentence names no action.
- *"…tickets and PRs and emails, we **don't** have a product, we have a spam
  cannon"* → **not** held.

**Then close the loop out loud:** "It also isn't a model deciding to be careful.
The model reads. A table decides. And the table has no branch that files a ticket
for a sentence the gate already threw away."

---

## 7. The PR-review beat

**The last forty seconds of the same standup — lines 28–36 of §4.** Not a second
meeting, not a second `--replay`, not a second Stop. Have the prop PR open in the
next window and keep talking; you never leave the recording.

> **YOU:** "Two more things and then I'll let you go eat."
> **THEM:** "I'm listening. Go."
> **YOU:** "Did you look at the beta signup page? Priya pushed it up this
> afternoon, the join the beta thing."
> **THEM:** "I skimmed it on my phone in the elevator. Copy's good."
> **YOU:** *"Yeah, the copy's fine. But on Priya's join button PR — **pull six** —
> that green is wrong. It should be our slate blue, **six B seven F nine nine**."*
> **THEM:** "Six B seven F nine nine. Right, the one out of the deck."
> **YOU:** "It's the only green anywhere on the site. It doesn't read as a brand
> colour, it reads as a bug someone hasn't noticed yet."
> **THEM:** "No, you're right. It's one line in the stylesheet, she can turn that
> around before Friday without breaking a sweat."
> **YOU:** "That's all I had. Go eat something."

**Say "pull six"** — spell the number the way you would in a room. That phrase
is the only licence for the PR number. **Say the hex as words, no "hash"** — the
quote has no `#` in it and the quote is what gets printed on GitHub.

One line out of these nine produces anything. What lands is an **inline
suggestion on the diff**, on `web/app/globals.css`, with a one-click Commit
button — and it lands from the *same* `--replay` that produced everything else,
because these nine lines are the tail of the demo tape rather than a tape of
their own.

### Why this beat is different, and how to say it

> "Nobody in that room said the word 'two-E-C-C-seven-one'. They said *that green
> is wrong*. The old value isn't in the transcript — it's in the diff. So it went
> and read the pull request, found the one colour this PR adds, and wrote the
> suggestion against that line."

That is literally what happens: `derive_old_value` scans the PR's **added** lines
for a value of the same shape as the one that was spoken, and accepts the answer
**only if the whole diff contains exactly one candidate**. Two candidates is
ambiguity, and ambiguity here means editing a line nobody talked about — so two
candidates produces no suggestion at all, and falls back to a plain PR comment
that says why in words. Context lines are never candidates: a review that
rewrites code the PR did not touch is a different and worse action.

If someone asks what happens when it *can't* be sure, that fallback is the
answer, and it is on the card:

> "``#2ecc71`` appears on 3 lines (`a:1`, `b:2`, `c:3`) — Adjourn did not guess
> which one was meant."

### The other thing this beat proves

That last line of the script — *"she can turn that around before Friday"* — used
to put a **calendar hold on an absent colleague's unpromised work**. "Can" is not
"will"; nobody asked her and she was not in the room to answer. It is now the
fourth shape in the prompt's non-actionable rule, and the corpus asserts this
beat fires the review and **nothing else**. If a founder asks how you find bugs
like that, the honest answer is: an auditor talked into it for forty seconds and
watched what came out.

### Between rehearsals

A submitted GitHub review **cannot be deleted** — only the executor's own undo
removes one, and only in the right order (blank the body while an inline comment
is still attached, *then* delete the comments; the reverse strands an
unremovable review on the PR forever). So:

```bash
$PY -m adjourn.tools.scrub_demo_world --rehearsal    # comments, reviews, labels
```

It reports anything it cannot remove rather than pretending.

---

## 8. What to say while the board fills in

### The first 30–50 seconds are silent. Plan for them.

**Measured tonight, not estimated.** Extraction on the **36-segment** demo
transcript — the standup with the PR beat merged into its tail — five cold runs
end to end in sim: **27.9s, 30.3s, 31.9s, 33.6s, 34.6s** — median **31.9s**. The
21 Aug re-measure on the merged tape: extraction **21.8s**, `REPLAY complete in
29.3s` including planning and firing. Live executors add roughly a second each (Linear
create 0.51s, Slack post 1.05s, one `gh` API round trip 0.52s) plus the draft PR,
which is three `gh` calls. **Budget 30 seconds in sim, up to 50 live.**

The variance does not track transcript length, so do not try to predict it.
Across the whole corpus tonight, extraction ran **3.9s to 55.5s**; the *smallest*
transcript in the set was not the fastest, and the same 29-segment transcript
took 40.2s, 45.1s, 47.9s and 55.5s on four different passes. Have four paragraphs ready, not one:

> "I didn't click anything. It's watching MeetingScribe's own status endpoint —
> the moment a recording stops, a transcription job appears, and that's the
> trigger."

> "What's happening right now is the expensive part. The transcription already
> happened on-device while we were talking. Now it's making four model calls in
> parallel to pull out what was actually *said* — decisions, assignments,
> promises — and nothing about this leaves the laptop. No transcript upload, no
> vendor, no retention policy to read."

> "Four calls, all four in parallel. The vocabulary they share was built by the
> pre-flight seed — that's what stops the same work item getting four different
> names in four different batches."

> "And notice what it is *not* doing: it is not deciding anything yet. The model
> only reads. The deciding is a table, and the table runs in about a
> millisecond."

⚠️ Do **not** say "the first one runs alone to seed the vocabulary." That is true
only during `--reseed` itself. On a machine where §1 step 6 has been run — i.e.
always, at the demo — the log says the opposite, in so many words: *"vocabulary
already seeded (3 topics) — every batch runs in parallel."* If a founder is
reading your terminal, the old line and the screen disagree.

Then the cards land together — which is why the two what-changed cards are
**pinned to the top** of the column rather than buried under the eight that fired
after them.

**When the #2 card lands** (top of Executed):
> "Last week we decided Redis. Tonight we reversed it. It didn't just summarize
> that — it went and found the issue where the old decision was written down and
> posted what changed, with the sentence that changed it. That's the graph doing
> the work: last week's standup is in memory, so tonight's meeting has something
> to contradict."

Click through to issue #2. The Before/After table is on GitHub.

**When SHA-5 moves** (also pinned high):
> "Eighty percent and 'should be in review' — that's a state transition. And
> here's the part I care about: a model read that sentence, and a *table* decided
> the ticket moves. The model never gets a vote on whether to act."

If someone asks what the threshold is, the true answer is a short ordered list:

> "In order. If the sentence names a destination — 'in review', 'ready to merge'
> — that wins. Then completion words: 'basically wrapped up', 'done', 'shipped
> it' all mean review. Then the percentage: ninety and up is review, twenty-five
> and up is in progress. And last, a work claim — 'I'm picking up SHA-6' — moves
> it to In Progress, but only from a column that hasn't started. Eighty on its
> own would only have said in progress. It went to review because he said
> 'should be in review'."

There is no rule anywhere that moves a ticket to **Done**. Say so; it is a good
answer. A human closes tickets.

**When Ready to send appears for Slack or email:**
> "Sending a message to humans is the one thing you can't quietly take back, so
> it does not go out on its own. Edit it if the wording is wrong. Press Send
> when you mean it. Don't send if you don't."

Press **Send**. Show your phone.

**Closing, on the recap:** click **Open recap** on the recap card. It is served
from the board's own origin at `/recap/<meeting-id>`, so it opens like any other
link. (It used to be a `file://` URL, which stock Chrome and Safari block
silently from an `http://` page — a bad thing to discover during your closing
beat.)

> "Everything you just watched is also a page on this laptop, with a verbatim
> quote under every card, and a ledger of who promised what. The audio and the
> full transcript never left the machine. The model calls are the only thing that
> touches a network, and the whole thing degrades to fixtures if they don't."

The page says this itself, and says it precisely: *"The audio, the full
transcript and this page never left this machine. Every card above cites the
sentence that caused it — and that one sentence is the only text that travels: to
the service the card wrote to, and to the graph behind the public board."* That
last clause appears **only when a cloud mirror is actually configured**. It is
there because the mirror does copy one verbatim sentence per fired action, and
the page used to claim the opposite.

The sandbox claim is load-bearing and literally true: extraction runs `claude -p`
with `--tools ""`, `--strict-mcp-config`, an empty MCP config,
**`--setting-sources ""`**, and an empty working directory. The fourth one is the
one that matters. Before that flag, this machine's own `~/.claude/settings.json`
ran a `UserPromptSubmit` hook that did a MongoDB Atlas vector search over every
prompt — i.e. over the transcript — and a `Stop` hook that uploaded distilled
memories afterwards. Five flags; the fourth is the fix.

**If someone asks "what did it decide *not* to do?"** — best question in the
room, and there are three answers on screen:

1. The **EXTRACTION** strip reads *"36 segments · 13 statements · 23 produced
   nothing"*. The twenty-three is now on the board, where you are pointing.
   (The counts move run to run — read the strip, do not quote this line.)
2. Any card you cancelled is still there, dimmed, badged **CANCELLED**, reading
   *"cancelled by a human — it never ran"* — and it stays cancelled: the
   reconcile pass will not quietly send it a minute later.
3. The **PLANNER** strip shows what the table declined and why.

---

## 9. Honest narration when something is not live

Use these verbatim. Every one is true, and each is a better line than pretending.

| Situation | Say this |
|---|---|
| A card is badged **SIM** | "Simulated — same code path, real payload, it just didn't make the last call. The badge is the product." |
| **Email** is sim (it always is tonight) | "No Gmail app password on this machine, so it renders the full message and stops. A missing credential is a mode here, not a crash." |
| The email card shows **`alex@example.com`** | Do not let this pass unremarked — it reads as a placeholder to anyone technical, right under "it built the exact payload". The card says so itself: *"simulated — no address on file"*. Better: put real addresses in `ADJOURN_ADDRESS_BOOK` in `adjourn/.env` before you start. |
| A **Linear ticket has no labels** | True and deliberate. The executor applies labels the workspace already has and **never creates one**, and the SHA workspace has no `from-meeting` label. "It won't invent a label in your workspace to tag its own work. That's a decision, not an omission." |
| Both **calendar holds** name the cache layer | "Ship: cache layer" and "Review: cache layer" — same work item, two commitments. If the review hold lands on **Fri 28 Aug**: "today is Friday, and 'let's review Friday' on a Friday means next Friday. A *deadline* of 'by Friday' would still mean today. Different reading for a different kind of sentence — and it's a table, not a guess." |
| **Extraction is slow** and you are out of script | "Four model calls in parallel through the Claude CLI, on-device transcription before that. That's the honest cost of not sending the transcript anywhere." Then §6 — the restraint corpus is a good thing to talk about while you wait. |
| **FalkorDB is down** | "It fell back to SQLite and told me so in the log. Same interface, same answers — the fallback is tested as a peer, not as an afterthought." |
| **A card is red / FAILED** | "That one didn't land, and it's saying so rather than hiding. Reconcile retries failed actions when the final transcript comes in — the fast pass runs on live captions, which are rough." |
| **A GitHub card refuses with "does not exist"** | The repo has not been seeded. That guard is doing its job: it checks the issue exists, lives in the allowlisted repo, and is not locked **before** it composes a comment. Run §0 step 2. |
| **The fast pass gets a word wrong** | "Live captions, two seconds after the stop. When the real transcript finishes it re-extracts, fixes the quotes in place, and fires only what's genuinely new. Nothing gets done twice." |
| **One beat is missing and the rest landed** | A single batch came back empty. Seen once tonight in 40+ corpus replays: `hallway-sync` produced 2 statements on every run but one, where it produced 0. This is the failure the Gemini key in §11 exists for — with it the ladder is claude → gemini → fixtures; without it a dead batch is a missing card. Re-run the replay, or narrate the beat and move on. |
| **Nothing fires at all** | "Then nothing was said that needed doing — which is most meetings. I'd rather it under-fire than invent work." And there will still be a recap saying exactly that, with the line count. |

---

## 10. Reset — and the order that matters

**Undo the live cards first, then reset.** `reset_demo` now *refuses* while live
actions are un-undone, and prints each one with its URL and the exact command to
reverse it:

```
[reset] REFUSING: 3 live action(s) have not been undone.
  github_update  https://github.com/sharique2004/adjourn/issues/2#issuecomment-…
    undo with: POST /undo/github_update:living-room-standup:issue-2:decision
```

That refusal exists because the old ordering was a trap: reset deleted
`~/.meetingscribe/executions.jsonl`, which holds the comment ids and branch names
the undo path needs, so once reset had run the rehearsal's live writes were
unreversible except by hand.

Two things changed:

- **The journal is archived, not deleted** — `executions.20260821-001233.jsonl`
  beside it. The undo payloads survive, so a stray comment you find tomorrow is
  still reversible.
- `--check` reports and changes nothing (exit 1 when blocked); `--force` proceeds
  after printing exactly what it is orphaning.

```bash
$PY -m adjourn.reset_demo --check      # would a reset run clean?
$PY -m adjourn.reset_demo              # journal archived, state cleared, memory kept
$PY -m adjourn.reset_demo --reseed     # …and rebuild last week's standup in both backends
```

Reset also sweeps orphan `Topic`/`Person`/`Ticket` nodes that `forget_meeting`
used to leave behind. Those matter beyond tidiness: known topics are fed back
into extraction as vocabulary, so a demo run after an unclean reset starts primed
with junk topics from an unrelated rehearsal — *"memory: 21 known topic(s)"*
where it should say three.

`reset_demo` does not touch GitHub. That is `scrub_demo_world --rehearsal`
(comments, reviews, labels) and `--teardown` (also closes the prop and the
roadmap and deletes the branch).

**Between a rehearsal and the real run this is not optional.** Every dedup key is
meeting-scoped, so a fresh meeting id gets a clean slate — but memory still holds
the prior standup you want to contradict, and a half-reset state (journal kept,
pending drafts from a previous run still waiting) will send someone else's
leftovers over the top of your demo.

---

## 11. Credential setup

Everything resolves through one accessor — `secrets_store.get_secret(name)` —
which reads the macOS Keychain first, then `ADJOURN_<NAME>`, then gives up and
puts that executor in sim mode. **Secrets never go in `adjourn/.env`.**

Run these yourself, in your own terminal. Each prompts for the value so it never
lands in shell history:

```bash
# Already provisioned and verified tonight — you should not need these.
security add-generic-password -a "$USER" -s adjourn.slack_bot_token -w
security add-generic-password -a "$USER" -s adjourn.slack_channel  -w   # "#all-test"
security add-generic-password -a "$USER" -s adjourn.linear_api_key -w   # lin_api_… (RAW, no Bearer)

# NOT provisioned. Email stays in sim mode until both of these exist.
security add-generic-password -a "$USER" -s adjourn.gmail_address      -w
security add-generic-password -a "$USER" -s adjourn.gmail_app_password -w   # 16 chars, from Google

# STRONGLY RECOMMENDED: the middle rung of the extraction ladder. Without it the
# ladder is claude → fixtures, and a real meeting has no fixture — so a single
# failed batch means an empty board. The key is already in drift/.env.
security add-generic-password -a "$USER" -s adjourn.gemini_api_key -w

# Optional: moves the cloud-mirror password out of adjourn/.env, where it is
# currently sitting in plaintext against that file's own advice.
security add-generic-password -a "$USER" -s adjourn.falkordb_cloud_password -w
```

Check what resolved on the **Connections** tab, or `$PY -m adjourn.secrets_store`.
If you add a secret mid-demo the process caches it — restart the orchestrator.

Secrets are also **quarantined out of the environment** at import: `config` lifts
every secret-shaped variable straight back out of `os.environ` into a private
dict, so the `claude` subprocess that reads your transcript cannot see the Slack
token, the Linear key or the cloud password even though it inherits everything
else. Verified with a child process reading its full environment.

**Address book.** A name in a meeting is not an email address, and Adjourn will
never guess one. The whole resolution table is one line in `adjourn/.env`:

```
ADJOURN_ADDRESS_BOOK="Alex=alex@example.com; Priya Nair=priya@example.com; Sam=sam@example.com"
```

Replace the `example.com` addresses **before the demo, not before the first live
send** — the sim card is what the room reads, and *"Would email
alex@example.com"* undercuts "it built the exact payload" in the same breath.

A name that is not in this table produces a failed card that names the person,
and the promise shows up in the recap's commitment ledger instead — the right
outcome, because emailing a guessed address is the one mistake you cannot
apologize for.

---

## 12. Live workspace facts

| | |
|---|---|
| **GitHub** | `sharique2004/adjourn` — the only repo any executor may write to, enforced by an allowlist that an `ADJOURN_REPO` env var **cannot widen** (a value outside the allowlist is ignored with a printed complaint). Issues #1–#5 are the roadmap; #6 is the prop PR. Before a live comment, the executor checks the issue exists, lives in the allowlisted repo, and is not locked. |
| **Linear** | Team `SHA` (Sharique Khatri). SHA-5 *streaming adapter* — **In Progress**, the move target. SHA-6 cache layer, SHA-7 retry logic, SHA-8 rate-limit. Team resolution **fails closed**: a missing key refuses rather than filing into a stranger's board. |
| **Slack** | Workspace **Test**, channel `#all-test`, bot user `adjourn`. The destination is **configuration, not payload** — a `channel` in the payload is ignored out loud, and an allowlist is checked before the send *and* before the delete. Post and delete verified live tonight. |
| **Calendar** | Not granted at the TCC level, so holds are written as `.ics` files in `adjourn/state/holds/` and badged SIM. Their Undo button reads **Undo (local)** — the `.ics` really is on disk, so it is a real undo. To go live: grant Terminal under System Settings → Privacy & Security → Calendars, then restart. |
| **Cloud mirror** | FalkorDB Cloud, **plaintext Redis** — the free tier does not speak TLS (measured: TLS connect times out at 4.36s where plaintext returns in 2.20s). Treat the password as disposable and rotate it after the demo. Runs on a daemon thread off the hot path; a mirror failure never delays or blocks an action. |
| **The board** | Binds `127.0.0.1:5117` and there is no `--host` flag to change that. `POST /undo` requires a per-run token, minted at startup, embedded in the page and sent as a header — so a page open in another tab cannot fire an undo at your board. |

---

## 13. Board-only demo (no MeetingScribe)

```bash
cd /path/to/adjourn
PY=python

$PY -m adjourn.reset_demo --reseed     # once, so the conflict beat has last week
$PY -m adjourn.board_server            # leave running — http://127.0.0.1:5117

# other terminal; MeetingScribe can be off:
ADJOURN_SIM=1 ADJOURN_LIVE_KINDS=github_update,pull_request_stub,pr_review_suggestion \
  $PY -m adjourn.orchestrator --replay --sim
```

**One bare `--replay` is the whole pitch.** With no target it runs the canned
`living-room-standup` transcript through the **real** extract → plan → execute
path, and that one tape produces all five kinds a judge is watching for:
`linear_create` (the webhook-signature ticket), `linear_move` (SHA-5 → In
Review), `slack_send` (Ready to send until you press Send), `github_update` (the Redis
decision on #2) and `pr_review_suggestion` (PR #6, `#6b7f99`, on the diff).
Do not run a second meeting for the PR beat — it is §4 lines 28–36.
A typo still refuses rather than reading MeetingScribe's live buffer.

The pipeline rail is **always on**, down the right of Follow-through — there is
nothing to expand. Watcher reads `replay · transcript ready`, Extraction ticks,
Planner shows what the table decided versus ignored, Executors show HOLDING /
LIVE / SIM as cards land. It survives the one-second fragment swap.

**The extraction bar really moves, and this is what it did.** Polled off
`/api/board` during the 21 Aug run on the merged tape, the batch counter was
written once per batch *as that batch came back* — the denominator first so the
bar exists during the longest single wait, then one step per completion:

```
20:57:54  batch 0/5 · 36 lines     ← denominator published before the first call
20:58:02  batch 1/5 · 8 lines      ← the serial seed batch, ~8s
20:58:10  batch 2/5 · 8 lines   ┐
20:58:10  batch 3/5 · 8 lines   ├ four parallel batches, landing together
20:58:10  batch 4/5 · 8 lines   ┘
20:58:15  batch 5/5 · 4 lines
20:58:16  done · 36 segments · 13 statements · 23 produced nothing
```

Two things follow from that shape. The bar sits at `0/5` for the first eight
seconds and that is honest — nothing has come back yet. And it can jump `1/5` →
`4/5` in one frame, because the parallel batches finish within the same second;
the number is how many have **completed**, which is the only reading of a
progress bar that never goes backwards on screen. If it ever sits frozen at
`0/N` for the whole pass, extraction is not batching — check the engine, not the
board.

The replayed meeting is also a row on **Meetings**, badged `REPLAY`, and its
transcript is where every card's quote links back to — that is beat E on the
`--replay` path. It is listed because `living-room-standup` is named in
`ADJOURN_PRESENTATION_MEETINGS`; a tape is never listed unless you list it.

**The run survives everything except a reset.** The journal is the authority, so
a cold open of `/` after the replay shows the same eleven cards — after a browser
reload, after the orchestrator process exits, and after the board server itself
is restarted. Verified 21 Aug: `pkill -f adjourn.board_server`, start it again,
`curl -s 127.0.0.1:5117/api/board` still lists all five kinds. The **only** thing
that clears it is `reset_demo`, which archives the journal on purpose; after that
the board falls back to the Last-adjourned panel reading those archived receipts.

---

## 14. If it goes wrong on stage

| Symptom | Do this |
|---|---|
| Nothing fires after the stop | First: has it been **30 seconds**? See §8. After that, `curl -s 127.0.0.1:5005/api/status` — is there a new key under `jobs`? Restart the watcher if it was down — it catches up any unprocessed-but-done meeting from the last hour. If a meeting was missed, catch it up with `$PY -m adjourn.orchestrator --replay <meeting-id>`. |
| The board is empty but a meeting *did* process | A meeting that yields nothing still writes a recap saying so, with the line count: *"Nothing followed from this meeting. N lines were spoken…"* A completely blank board with no header means the orchestrator is not running. |
| Extraction produces nothing | `ADJOURN_FIXTURE_FLOOR=1` makes the demo meeting fall back to its hand-written ground truth. **Only for the demo meeting** — it must stay off for a real one. |
| A card fires you did not want | Click **Undo**. GitHub comments, Linear moves, Slack messages and PR reviews all reverse for real, and the recap page is rewritten so it agrees with the board — the undone row goes struck through with an UNDONE badge and its dead permalink is printed as text rather than linked. Email does not reverse: Don't send before it goes out; after Send it cannot come back, and the button says so. |
| The Undo button says **Undo (sim)** | Nothing left the machine; there is nothing out there to take back. |
| The Undo button says **Undo (local)** | A SIM-badged calendar hold or recap whose file really is on disk. That is a real undo of a real local artifact. |
| A draft you do not want to send | Click **Don't send**. Nothing left the machine. Undo is for something that already happened. |
| Undo returns **403** | The board was restarted after the page was loaded, so the page is holding last run's token. Reload the board; the message says so. |
| The board is stale | It polls once a second and only swaps HTML when a version hash changes. Reload if you must; nothing is lost — it renders from the journal. |
| Everything is on fire | Re-run with **`--sim-all`** (not `--sim` — see §2) and a bare **`--replay`**. The whole demo runs off the fixture transcript through the identical code path: **~30s to the cards**. Slack and email wait in Ready to send until you press Send. |
| You typo the `--replay` target | It refuses rather than guessing. A path, a folder with no `meeting.json`, or an unknown meeting id all print a refusal and fire nothing — because falling through would read the engine's *current* caption buffer and file another meeting's words under your typo. |

**Full dress rehearsal, any time:**

```bash
$PY -m adjourn.reset_demo --reseed
ADJOURN_SIM=1 ADJOURN_LIVE_KINDS=github_update,pull_request_stub,pr_review_suggestion \
  $PY -m adjourn.orchestrator --replay
```

Run it twice. The second run must fire **zero** duplicate actions — only the
recap page rewrites itself, which it is supposed to do. That is the dedup
guarantee, and it is the thing most worth re-checking before you walk on.

**If the FalkorDB Cloud graph is part of the pitch**, check it before you walk on
— the mirror is deliberately best-effort and eventually consistent:

```bash
$PY -m adjourn.cloud_mirror --probe
```

The drain budget scales with the queue (3s per outstanding write, capped at 30s)
and the exit line reports the true backlog depth. A non-zero backlog drains on
the next run's first write.

---

## 15. Numbers, measured — not estimated

Everything here was measured on this machine on 21 Aug 2026. Re-measure rather
than trusting the table if the machine or the network changed.

| | |
|---|---|
| Extraction, demo transcript (36 segments, PR beat included), 5 cold runs | 27.9 / 30.3 / 31.9 / 33.6 / 34.6 s — median **31.9s**; re-measured 21 Aug at **21.8s** |
| Extraction, whole 9-scenario corpus | **3.9s – 55.5s**, uncorrelated with transcript length |
| Statements from the demo transcript, 6 runs | 11–13, yielding 8–10 acting cards |
| Live executor round trips | Linear create 0.51s · Linear archive 0.40s · Slack post 1.05s · Slack delete 0.36s · one `gh` API call 0.52s |
| Cloud mirror probe | 2.20s plaintext (TLS times out at 4.36s — the free tier has none) |
| Full test suite | **16 suites, ~70s**, `python -m adjourn.tests.run_all` |
| Restraint corpus | **9/9 scenarios as specified**, ~180s of extraction |
| Star card position, 1512×900 | top at 325px — 2 cards fully above the fold |
| Star card position, 864×514 (175% zoom) | 269–462px — fully above the fold |
| Regret window | 60s, `REGRET_WINDOW_SECONDS` in `adjourn/.env` |
| Board poll | once per second, HTML swapped only on a version-hash change |
