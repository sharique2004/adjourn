# PR_REVIEW_PROP — the standing prop for the PR-review beat

The PR-review beat needs a real pull request sitting on a real repo, open, with a
real wrong colour in its diff. This is that pull request. It is **scenery**: it
stays open through demo night, it is never merged, and the only thing that ever
changes about it is that Adjourn's review gets wiped off it between rehearsals.

It is a pull request **against Adjourn's own landing page**, in Adjourn's own
repository. That is deliberate. A demo repo built to be reviewed is a stage set;
a pull request that would actually change the site the audience is looking at is
not. Every link in this beat goes somewhere a sceptic can read.

## The prop

| | |
| --- | --- |
| **Repo** | `sharique2004/adjourn` — the only repo any executor may write to |
| **URL** | https://github.com/sharique2004/adjourn/pull/6 |
| **Title** | `[demo-prop] Add join button to landing page` |
| **State** | Open, **not** a draft, mergeable. Do not merge it. |
| **Branch** | `priya/join-button` → base `main` |
| **Files** | `web/app/globals.css`, `web/app/page.tsx` |
| **The line** | `web/app/globals.css` line **33** — `  --join-accent: #2ecc71;` |

> The pull request number and the spoken number in the transcript are kept in
> step automatically. `adjourn.tools.seed_demo_world` rewrites this file, the
> `.jsonl` and the `.fixtures.json` to whatever number GitHub actually assigns.
> If you rebuild the prop by hand, run that script rather than editing three
> files and hoping.

### The two hex values

| | Value | Where it comes from |
| --- | --- | --- |
| **Old** (wrong) | `#2ecc71` | **The diff only.** It is not spoken anywhere in the transcript. |
| **New** (correct) | `#6b7f99` | **The transcript.** Spoken as "six B seven F nine nine". |

That asymmetry is the point of the beat, not an oversight — see
[The one thing to get right](#the-one-thing-to-get-right) below.

`#2ecc71` appears **exactly once in the pull request's own diff**, on its own
line, so the correction is a clean one-line change with nothing to disambiguate:

```css
:root {
  ...
  --amber: #e3b341;

  /* Beta CTA accent. Green so the signup reads as a "go" rather than another
     amber link — happy to be overruled on that. */
  --join-accent: #2ecc71;          /* <- the only occurrence in the diff */
  --join-accent-ink: #08120c;
```

Every use goes through `var(--join-accent)` — the button fill, its border, the
hover, the focus ring. Changing that one line recolours the whole CTA, which is
what makes the correction look surgical on screen.

`seed_demo_world` refuses to build the branch unless that count is exactly one,
and unless the hex appears nowhere else under `web/`. It is checked before the
branch exists rather than discovered while the beat is running.

## The line to speak aloud

Fixture segment `prb-s05`, spoken by **You**:

> Yeah, the copy's fine. But on Priya's join button PR — pull five — that
> green is wrong. It should be our slate blue, six B seven F nine nine.

Say the number the way it is written above — the spoken words, not "PR five" and
not "hashtag five". That phrase is the only reason `entity_refs.issue_number` is
allowed to hold it: the fixture rule in `fixtures/README.md` is that refs carry
only handles spoken *in that segment*, and a number nobody says is a number the
extractor invented.

Say the hex **as words** — "six B seven F nine nine". Do not say "hash six B
seven F nine nine". The fixture quote has no `#` in it, and the quote is what the
board prints under the action card.

The surrounding chatter in `pr-review-beat.jsonl` (`prb-s01` … `prb-s09`) is there
so the beat does not sound like a command being dictated to a machine. Only
`prb-s05` carries a statement; the rest is meant to be talked over or skipped.

## The one thing to get right

**The old hex is not in the conversation.** Nobody says "two E C C seven one" —
they say *"that green is wrong"*. The only place `#2ecc71` exists is
`web/app/globals.css` in the PR's diff.

So the beat only works if whatever handles it **reads the diff**. An extractor
that only sees the transcript can produce "should be `#6b7f99`" and cannot
produce "was `#2ecc71`". That is why the ground-truth `claim` names the new value
and *describes* the old one. The old value lives in the `_extras` block of
`pr-review-beat.fixtures.json` with `old_value_provenance: "diff"` stamped on it,
so nobody later mistakes it for something the words gave us.

If the demo shows a before/after with both hexes in it, that is evidence the
agent opened the code. If it shows both without reading the diff, the fixture is
lying and the beat is a magic trick.

## Reset between rehearsals

`adjourn.reset_demo` clears the **local** side — the journal, `state/`, recaps,
holds — and its docstring is explicit that it never touches GitHub. The GitHub
side is `adjourn.tools.scrub_demo_world`.

Run the local reset first. Order matters: the journal at
`~/.meetingscribe/executions.jsonl` is the dedup authority, so a rehearsal that
leaves it populated will decline to act the second time and the beat will look
broken when it is merely being polite.

```bash
cd /path/to/adjourn

# 1. local: journal, pending countdowns, pipeline, recaps, holds
python -m adjourn.reset_demo

# 2. GitHub: every comment, review comment and label Adjourn left tonight
python -m adjourn.tools.scrub_demo_world --rehearsal

# 3. verify — the scrub prints the same counts it checked
python -m adjourn.tools.scrub_demo_world --rehearsal --dry-run
```

The scrub removes comments from every open issue and pull request in the repo,
strips the two Adjourn labels, and leaves the roadmap issues and the prop
standing. It never touches a repository outside `config.ALLOWED_GITHUB_REPOS`.

### The reset trap: submitted reviews cannot be deleted

The scrub deletes **comments**. GitHub does not allow a submitted **review** to
be deleted at all — `DELETE /pulls/{n}/reviews/{id}` works only while a review is
still `PENDING`:

* `COMMENTED` reviews can be neither deleted nor dismissed — permanent.
* `APPROVED` / `CHANGES_REQUESTED` can be *dismissed*, which leaves a visible
  greyed entry in the timeline. Still permanent.

There is exactly one way to take a submitted review back, and the order is the
whole trick — it works only because GitHub garbage-collects a `COMMENT` review
that ends up with an empty body **and** no inline comments:

```
1. PUT    /repos/{repo}/pulls/{n}/reviews/{id}  body=""   # legal only while >=1 comment is attached
2. DELETE /repos/{repo}/pulls/comments/{comment_id}       # for each inline comment
```

Doing those two in the other order strands an un-removable review on the PR
forever. `pr_review_suggestion_executor.undo()` implements exactly this order and
verifies the review is gone with a follow-up `GET` that must 404.

That finding is also why the executor's *uncertain* path — zero or many matches —
posts a plain issue comment rather than a body-only review: a body-only `COMMENT`
review cannot be taken back by any means the API offers, and the path that fires
when Adjourn is least sure must be the one that is easiest to reverse.

Check before each run:

```bash
gh api repos/sharique2004/adjourn/pulls/6/reviews \
  --jq '.[] | "\(.id)\t\(.state)\t\(.user.login)"'   # expect: empty
```

## Rebuilding the prop from scratch

The branch is fully described by two anchored insertions in
`adjourn/tools/github_world.py`, so recreating it is one command:

```bash
gh pr close 6 --repo sharique2004/adjourn --delete-branch
python -m adjourn.tools.seed_demo_world
```

The new PR gets a new number, and the seed script rewrites the spoken line in
`pr-review-beat.jsonl`, the `quote` / `claim` / `entity_refs.issue_number` /
`_extras` in `pr-review-beat.fixtures.json`, and this file, all from that one
number. The quote and the transcript segment stay byte-identical because both
are written from the same string.

If the landing page has moved on and an anchor no longer matches, the script
stops and names the anchor rather than overwriting the file with a stale
snapshot. Fix the anchor in `github_world.PROP_PATCHES` and run it again.
