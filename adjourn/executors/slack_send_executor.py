"""slack_send — post the message someone promised to send. (Lane D)

Payload shape:
    {
      "text": "...",                     # what was promised, in the speaker's words
      "blocks": [],                      # optional Block Kit; built here when absent
      "recap_url": "file:///...",        # optional; resolved from the recap page
      "human_preview": "Slack #eng: heads up, cache layer is moving to Redis"
    }

REGRET WINDOW: 60s. The planner sets regret_window_s, regret_window.py holds this
in state/pending.json, and the board draws a countdown ring with a cancel button.
By the time execute() is called the human has already had their chance — this
module never asks a question, it sends.

Live transport: POST https://slack.com/api/chat.postMessage
    Authorization: Bearer xoxb-...      (Slack DOES use "Bearer " — unlike Linear)
    Content-type: application/json; charset=utf-8
Slack returns HTTP 200 even on failure — check the `ok` field in the JSON body,
never the status code.

Undo: chat.delete with the channel + ts from undo_payload. A bot can delete its
own messages, so undo is real here rather than cosmetic.

=============================================================================
 SAFETY: NEVER post to the wellx-ai workspace found on this Mac. That is
 someone's real employer. Live sends are allowed only against a token
 provisioned specifically for Sharique's own demo workspace, and every live
 send calls auth.test FIRST and runs the team name past
 assert_workspace_is_allowed() before a single message goes out. This module
 also never reads a Slack token from anywhere except get_secret() — no
 ~/.slack, no keychain sweep, no environment scavenging.

 THE DESTINATION IS NOT IN THE PAYLOAD. `resolve_channel` reads the channel
 from the secret store and the config default and nowhere else, a `channel`
 key on the payload is ignored out loud, and `assert_channel_is_allowed`
 checks the answer against config.ALLOWED_SLACK_CHANNELS before the send and
 again before the undo. Which workspace is guarded by one check; which room
 inside it is guarded by the other.
=============================================================================

Secrets: slack_bot_token and slack_channel, both unconditional. Either absent
=> sim mode, loudly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import config, results, secrets_store

if TYPE_CHECKING:
    from ..planner import Action

ACTION_KIND = "slack_send"

# Canonical name, now defined in secrets_store alongside every other secret name.
# Re-exported here because this module's callers already import it from here.
SLACK_CHANNEL = secrets_store.SLACK_CHANNEL
REQUIRED_SECRETS: tuple[str, ...] = (secrets_store.SLACK_BOT_TOKEN, SLACK_CHANNEL)

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
DELETE_MESSAGE_URL = "https://slack.com/api/chat.delete"
AUTH_TEST_URL = "https://slack.com/api/auth.test"
PERMALINK_URL = "https://slack.com/api/chat.getPermalink"
HTTP_TIMEOUT_SECONDS = 10

# Shown on the sim card when no channel is configured anywhere, so the preview is
# still a complete, readable message rather than a hole. It is the allowlisted
# default rather than an invented room: a sim card's whole job is to be the exact
# thing the live call would have sent, and naming a channel Adjourn is not
# allowed to post to would make the preview wrong in the one way that matters.
PLACEHOLDER_CHANNEL = config.DEFAULT_SLACK_CHANNEL

# Workspaces this executor must never post into, whatever the token says.
FORBIDDEN_WORKSPACE_NAMES = frozenset({"wellx-ai"})


def required_secrets_for(action: Action) -> tuple[str, ...]:
    """Secrets a LIVE send of this particular action needs.

    Unconditional, both of them. This used to waive the slack_channel secret for
    a payload that named its own channel, which made the payload — a structure
    the model contributes to — the thing that decided where a message went. The
    destination is configuration now, so its source is always required.
    """
    del action  # the answer no longer depends on the payload; that is the fix
    return REQUIRED_SECRETS


def resolve_channel(action: Action) -> str:
    """Where this goes. Configuration only — the payload gets no vote.

    A `channel` on the payload is ignored, loudly. Adjourn posts to exactly one
    room and that room is named in the secret store and the config allowlist; a
    transcript that says "put this in #general", or a model that decides to add a
    field, must not be able to move a message. Returns "" when nothing is
    configured, which the caller renders as a sim card.
    """
    payload = action.payload or {}
    named = str(payload.get("channel") or "").strip()
    configured = (
        secrets_store.get_secret(SLACK_CHANNEL) or config.slack_channel() or ""
    ).strip()
    if named and named != configured:
        print(
            f"[{ACTION_KIND}] ignoring payload channel {named!r} — "
            f"the destination is configuration, not payload"
        )
    return configured


def assert_channel_is_allowed(channel: str) -> None:
    """Guard: refuse to send to any channel outside the allowlist.

    The GitHub allowlist's twin, and it exists for the same reason: "Slack only
    ever posts to #all-test" should be something a reader can verify by finding
    the check, not something they have to trust by tracing every path a channel
    name could have travelled.
    """
    candidate = (channel or "").strip()
    if candidate not in config.ALLOWED_SLACK_CHANNELS:
        raise PermissionError(
            f"refusing live Slack send to {channel!r}; "
            f"allowed: {sorted(config.ALLOWED_SLACK_CHANNELS)}"
        )


def resolve_recap_reference(action: Action) -> str:
    """A link to this meeting's local recap page, or "" when there is none yet.

    The recap lives on disk, so this is a file:// URL — local-first means the
    receipt for an action is a file the user owns, not a page on someone's SaaS.
    """
    payload = action.payload or {}
    explicit = str(payload.get("recap_url") or "").strip()
    if explicit:
        return explicit
    if not action.meeting_id:
        return ""
    try:  # recap_page_executor is another lane's module; never hard-depend on it
        from . import recap_page_executor

        return recap_page_executor.recap_path_for(action.meeting_id).as_uri()
    except Exception:  # noqa: BLE001
        return (config.recaps_directory() / f"{action.meeting_id}.html").as_uri()


def build_message_body(action: Action) -> dict:
    """The chat.postMessage JSON body. Pure — byte-identical in live and sim.

    Leads with the quote and who said it: this should read as a record of the
    meeting, not as a bot announcing itself. The promised message (payload text)
    follows the attribution, and the recap link goes in a context block where it
    does not compete with the sentence a human actually said.
    """
    payload = action.payload or {}
    channel = resolve_channel(action) or PLACEHOLDER_CHANNEL
    speaker = action.speaker or "Someone"
    promised = str(payload.get("text") or "").strip()
    quote = (action.quote or "").strip()
    recap_url = resolve_recap_reference(action)

    if quote and promised:
        text = f'{speaker} in the meeting: “{quote}”\n\n{promised}'
    elif quote:
        text = f'{speaker} in the meeting: “{quote}”'
    else:
        text = promised or "(no message text)"

    blocks = payload.get("blocks")
    if not blocks:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        context_parts = ["Sent by Adjourn the moment the meeting ended"]
        if recap_url:
            context_parts.append(f"<{recap_url}|meeting recap>")
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " · ".join(context_parts)}],
        })

    return {
        "channel": channel,
        "text": text,           # notification fallback; required even with blocks
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
    }


def assert_workspace_is_allowed(team_name: str) -> None:
    """Guard: refuse to post into a forbidden workspace. Implemented on purpose."""
    if (team_name or "").strip().lower() in FORBIDDEN_WORKSPACE_NAMES:
        raise PermissionError(
            f"refusing to post to workspace {team_name!r} — not Sharique's demo workspace"
        )


def execute(action: Action) -> results.ExecutorResult:
    """Post the message. Assumes the regret window has already elapsed."""
    mode = secrets_store.decide_mode(required_secrets_for(action), label=ACTION_KIND)
    body = build_message_body(action)

    # --- TRANSPORT SWAP ---
    if mode == results.MODE_LIVE:
        token = secrets_store.get_secret(secrets_store.SLACK_BOT_TOKEN) or ""
        try:
            assert_channel_is_allowed(body["channel"])
            posted = _post_message_live(body, token)
        except PermissionError as error:
            # The workspace guard fired. Nothing was sent; say so and stay sim.
            print(f"[{ACTION_KIND}] BLOCKED: {error}")
            return results.ExecutorResult.failed(
                ACTION_KIND, f"blocked before sending: {error}",
                quote=action.quote, speaker=action.speaker, meeting_id=action.meeting_id,
            )
        except Exception as error:  # noqa: BLE001
            # A failure here may or may not have landed a message, so the card is
            # badged live: it must admit that something might be out there.
            return results.ExecutorResult.failed(
                ACTION_KIND, f"Slack send failed: {error}",
                mode=results.MODE_LIVE,
                quote=action.quote, speaker=action.speaker, meeting_id=action.meeting_id,
            )
        return results.ExecutorResult(
            ok=True,
            kind=ACTION_KIND,
            external_id=posted["ts"],
            url=posted.get("permalink"),
            human_summary=f"Posted to {body['channel']} — {_one_line(body['text'])}",
            mode=results.MODE_LIVE,
            undo_payload={
                "channel": posted["channel"],       # the id, which chat.delete wants
                "channel_name": body["channel"],    # the name, which undo can check
                "ts": posted["ts"],
            },
            quote=action.quote,
            speaker=action.speaker,
            meeting_id=action.meeting_id,
        )

    return _render_simulated(action, body)


def undo(result: results.ExecutorResult) -> bool:
    """Delete the posted message via chat.delete (channel + ts from undo_payload)."""
    if result.mode == results.MODE_SIM:
        print(f"[{ACTION_KIND}] simulated message never left the machine — nothing to delete")
        return True

    payload = result.undo_payload or {}
    channel, timestamp = payload.get("channel"), payload.get("ts")
    if not channel or not timestamp:
        print(f"[{ACTION_KIND}] cannot undo — no channel/ts on the record")
        return False

    # The allowlist guards the way back out as well as the way in. `channel` here
    # is Slack's id (C…), which this token cannot resolve to a name, so the check
    # runs against the name the send recorded beside it. A record with an id and
    # no name is from before that was written down, or was edited: refuse it and
    # say so rather than firing a delete at an unverified room.
    channel_name = str(payload.get("channel_name") or "").strip()
    if not channel_name:
        print(f"[{ACTION_KIND}] cannot undo — record has a channel id but no channel name")
        return False
    try:
        assert_channel_is_allowed(channel_name)
    except PermissionError as error:
        print(f"[{ACTION_KIND}] {error}")
        return False

    token = secrets_store.get_secret(secrets_store.SLACK_BOT_TOKEN)
    if not token:
        print(f"[{ACTION_KIND}] cannot undo — the bot token is gone")
        return False
    try:
        import requests

        response = requests.post(
            DELETE_MESSAGE_URL,
            json={"channel": channel, "ts": timestamp},
            headers=_authorized_headers(token),
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        answer = response.json()
    except Exception as error:  # noqa: BLE001
        print(f"[{ACTION_KIND}] chat.delete failed: {error}")
        return False
    if not answer.get("ok"):
        print(f"[{ACTION_KIND}] chat.delete refused: {answer.get('error')}")
        return False
    return True


def _one_line(text: str, limit: int = 90) -> str:
    """First line of a message, trimmed, for a card summary."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _authorized_headers(token: str) -> dict:
    """Slack wants the "Bearer " prefix. Linear does not. This is the Slack one."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }


# --- TRANSPORT SWAP (bottom of the module) ----------------------------------


def _post_message_live(body: dict, bot_token: str) -> dict:
    """The only outbound send in this module. Returns {"ts", "channel", "permalink"}.

    Order matters: auth.test FIRST, so the workspace guard runs before any message
    exists. Slack answers HTTP 200 for errors, so every response is judged by its
    `ok` field.
    """
    import requests

    headers = _authorized_headers(bot_token)

    identity = requests.post(AUTH_TEST_URL, headers=headers, timeout=HTTP_TIMEOUT_SECONDS).json()
    if not identity.get("ok"):
        raise RuntimeError(f"auth.test failed: {identity.get('error')}")
    assert_workspace_is_allowed(identity.get("team", ""))
    print(f"[{ACTION_KIND}] LIVE — posting to {identity.get('team')} as {identity.get('user')}")

    response = requests.post(
        POST_MESSAGE_URL, json=body, headers=headers, timeout=HTTP_TIMEOUT_SECONDS
    )
    answer = response.json()
    if not answer.get("ok"):
        raise RuntimeError(f"chat.postMessage failed: {answer.get('error')}")

    timestamp, channel_id = answer["ts"], answer.get("channel", body["channel"])
    permalink = None
    try:  # best effort: a missing permalink must not fail a message that went out
        link = requests.get(
            PERMALINK_URL,
            params={"channel": channel_id, "message_ts": timestamp},
            headers=headers,
            timeout=HTTP_TIMEOUT_SECONDS,
        ).json()
        permalink = link.get("permalink") if link.get("ok") else None
    except Exception:  # noqa: BLE001
        permalink = None
    return {"ts": timestamp, "channel": channel_id, "permalink": permalink}


def _render_simulated(action: Action, body: dict) -> results.ExecutorResult:
    """Sim mode: the exact Slack message that would have gone out, unsent."""
    return results.ExecutorResult.simulated(
        ACTION_KIND,
        f"Would post to {body['channel']} — {_one_line(body['text'])}",
        rendered_payload={
            "url": POST_MESSAGE_URL,
            "method": "POST",
            "headers": {"Authorization": "Bearer <slack_bot_token>",
                        "Content-Type": "application/json; charset=utf-8"},
            "body": body,
        },
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
    )
