"""email_send — send the follow-up email someone promised. (Lane D)

Payload shape:
    {
      "to": ["div@example.com"],       # explicit addresses win over the address book
      "person": "Div",                 # or entity_refs.person — resolved via the book
      "cc": [],
      "subject": "Follow-up: cache layer timeline",
      "body_text": "...",              # plain text; the verbatim quote is added here
      "recap_path": "/…/recaps/m1.html",   # optional; inlined below the message
      "human_preview": "Email Div: cache layer timeline"
    }

ADDRESS BOOK. A name in a meeting ("I'll email Div tonight") is not an address.
Resolution is a small deterministic table in adjourn/.env:

    ADJOURN_ADDRESS_BOOK="Div=div@example.com; Priya=priya@example.com"

Separators are ';' or newlines; matching is case-insensitive on the whole name
and on the first name. A name that is NOT in the book is never guessed at — the
card fails and says which name it could not resolve. Emailing a guessed address
is the one failure mode that cannot be undone or apologised for.

REGRET WINDOW: 60s, and it matters more here than anywhere else.

=============================================================================
 EMAIL IS IRREVERSIBLE. There is no unsend. undo() returns False and says so;
 it does NOT pretend. The countdown IS the undo for this executor — which is
 exactly why the regret window exists and why the board's ring is visible.
=============================================================================

Live transport: stdlib smtplib + email.message.EmailMessage.
    smtp.gmail.com:587, STARTTLS, login with a 16-character Gmail app password.
    No third-party SDK, no API key in a header — this is deliberately boring.

Secrets: gmail_address + gmail_app_password. Either absent => sim mode, loudly.
"""

from __future__ import annotations

import re
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import TYPE_CHECKING

from .. import config, results, secrets_store

if TYPE_CHECKING:
    from ..planner import Action

ACTION_KIND = "email_send"
REQUIRED_SECRETS: tuple[str, ...] = (
    secrets_store.GMAIL_ADDRESS,
    secrets_store.GMAIL_APP_PASSWORD,
)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_TIMEOUT_SECONDS = 20

ADDRESS_BOOK_SETTING = "ADJOURN_ADDRESS_BOOK"
# Used only to render a complete sim preview when no gmail_address is provisioned.
PLACEHOLDER_FROM_ADDRESS = "sharique.khatri@gmail.com"
MESSAGE_ID_DOMAIN = "adjourn.local"
# A recap larger than this is linked rather than inlined — nobody wants a 4MB email.
MAX_INLINE_RECAP_BYTES = 200_000

_EMAIL_SHAPED = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HTML_BODY = re.compile(r"<body[^>]*>(.*)</body>", re.IGNORECASE | re.DOTALL)


# --- the address book -------------------------------------------------------


def read_address_book() -> dict[str, str]:
    """{"div": "div@example.com", ...} from ADJOURN_ADDRESS_BOOK. Empty when unset.

    Deterministic parsing, no cleverness: "Name=address" pairs separated by ';'
    or newlines. Malformed entries are skipped rather than raised — a typo in
    .env must not take the executor down mid-demo.
    """
    raw = config.read_setting(ADDRESS_BOOK_SETTING)
    book: dict[str, str] = {}
    for entry in re.split(r"[;\n]+", raw):
        entry = entry.strip().strip(",")
        if not entry or "=" not in entry:
            continue
        name, _, address = entry.partition("=")
        name, address = name.strip(), address.strip().strip("<>")
        if name and _EMAIL_SHAPED.match(address):
            book[name.lower()] = address
    return book


def look_up_address(name: str, book: dict[str, str] | None = None) -> str:
    """A person's address from the book: full name first, then first name. "" if unknown."""
    book = read_address_book() if book is None else book
    key = (name or "").strip().lower()
    if not key:
        return ""
    if key in book:
        return book[key]
    first = key.split()[0]
    return book.get(first, "")


def resolve_recipients(action: Action) -> tuple[list[str], list[str]]:
    """(addresses, unresolved_names). Explicit addresses win; names go through the book."""
    payload = action.payload or {}
    entity_refs = payload.get("entity_refs") or {}

    addresses: list[str] = []
    unresolved: list[str] = []

    for candidate in _as_list(payload.get("to")):
        if _EMAIL_SHAPED.match(candidate):
            addresses.append(candidate)
        else:
            unresolved.append(candidate)

    named = _as_list(payload.get("person")) + _as_list(entity_refs.get("person"))
    book = read_address_book()
    for person in named:
        if _EMAIL_SHAPED.match(person):
            addresses.append(person)
            continue
        found = look_up_address(person, book)
        if found:
            addresses.append(found)
        else:
            unresolved.append(person)

    # Re-resolve anything that came in through "to" but was a name, not an address.
    still_unresolved: list[str] = []
    for name in unresolved:
        found = look_up_address(name, book)
        if found:
            addresses.append(found)
        else:
            still_unresolved.append(name)

    seen: set[str] = set()
    unique = [a for a in addresses if not (a.lower() in seen or seen.add(a.lower()))]
    return unique, still_unresolved


def _as_list(value) -> list[str]:
    """Accept a string, a list, or None from the payload without complaining."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(item).strip() for item in value if str(item).strip()]


# --- pure message construction ----------------------------------------------


def build_subject(action: Action) -> str:
    """The subject line, from the payload or from what was actually said."""
    payload = action.payload or {}
    explicit = str(payload.get("subject") or "").strip()
    if explicit:
        return explicit
    topic = str(payload.get("topic") or "").strip()
    if topic:
        return f"Follow-up: {topic}"
    words = (action.quote or "").strip().rstrip(".").split()
    if words:
        return "Follow-up: " + " ".join(words[:8]) + ("…" if len(words) > 8 else "")
    return "Follow-up from our meeting"


def build_body_text(action: Action) -> str:
    """The plain-text body. Carries the verbatim commitment — that is the receipt."""
    payload = action.payload or {}
    lines: list[str] = []
    written = str(payload.get("body_text") or "").strip()
    if written:
        lines.append(written)
    if action.quote:
        speaker = action.speaker or "someone"
        lines.append("")
        lines.append(f'This was promised in the meeting — {speaker} said: "{action.quote}"')
    lines.append("")
    lines.append("— sent automatically by Adjourn when the meeting ended.")
    return "\n".join(lines).strip() + "\n"


def read_recap_html(action: Action) -> str:
    """The recap page's HTML, if one has been written and it is small enough. "" otherwise."""
    payload = action.payload or {}
    explicit = str(payload.get("recap_path") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
    elif action.meeting_id:
        path = config.recaps_directory() / f"{action.meeting_id}.html"
    else:
        return ""
    try:
        if not path.is_file() or path.stat().st_size > MAX_INLINE_RECAP_BYTES:
            return ""
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def build_html_body(body_text: str, recap_html: str) -> str:
    """The HTML alternative: the message, then the recap inlined beneath a rule.

    The recap is inlined rather than attached because an attachment is a file the
    recipient has to decide to open; the point is that the follow-up carries its
    own evidence.
    """
    paragraphs = "".join(
        f"<p>{_escape_html(block)}</p>"
        for block in body_text.strip().split("\n\n")
        if block.strip()
    )
    inlined = ""
    if recap_html:
        match = _HTML_BODY.search(recap_html)
        inlined = (
            "<hr style=\"margin:28px 0;border:0;border-top:1px solid #ddd\">"
            "<p style=\"color:#666;font-size:13px\">Meeting recap</p>"
            + (match.group(1) if match else recap_html)
        )
    return (
        '<div style="font-family:-apple-system,Helvetica,sans-serif;'
        'font-size:15px;line-height:1.5;color:#111">'
        f"{paragraphs}{inlined}</div>"
    )


def _escape_html(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace("\n", "<br>")
    )


def build_email_message(action: Action, from_address: str) -> EmailMessage:
    """Build the EmailMessage. Pure — the identical object in live and sim.

    Raises ValueError when no recipient can be resolved. Guessing an address is
    the one mistake this product must never make.
    """
    payload = action.payload or {}
    to_addresses, unresolved = resolve_recipients(action)
    if not to_addresses:
        raise ValueError(
            "no recipient could be resolved"
            + (f" (unknown in the address book: {', '.join(unresolved)})" if unresolved else "")
        )

    message = EmailMessage()
    message["From"] = from_address
    message["To"] = ", ".join(to_addresses)
    cc_addresses = [a for a in _as_list(payload.get("cc")) if _EMAIL_SHAPED.match(a)]
    if cc_addresses:
        message["Cc"] = ", ".join(cc_addresses)
    message["Subject"] = build_subject(action)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=MESSAGE_ID_DOMAIN)

    body_text = build_body_text(action)
    message.set_content(body_text)

    recap_html = read_recap_html(action)
    message.add_alternative(build_html_body(body_text, recap_html), subtype="html")
    return message


def render_email_preview(message: EmailMessage) -> dict:
    """Flatten an EmailMessage for the board card — and the full RFC822 for sim.

    Sim mode shows the ENTIRE message source, headers and all. That is the honesty
    of the mode: what you are looking at is exactly what would have been handed to
    smtplib, byte for byte.
    """
    body_part = message.get_body(preferencelist=("plain",))
    return {
        "from": message["From"],
        "to": message["To"],
        "cc": message["Cc"] or "",
        "subject": message["Subject"],
        "message_id": message["Message-ID"],
        "body": body_part.get_content() if body_part else "",
        "rfc822": message.as_string(),
        "transport": f"smtp://{SMTP_HOST}:{SMTP_PORT} STARTTLS",
    }


# --- execute / undo ---------------------------------------------------------


def execute(action: Action) -> results.ExecutorResult:
    """Send the email. Assumes the regret window has already elapsed."""
    mode = secrets_store.decide_mode(REQUIRED_SECRETS, label=ACTION_KIND)
    from_address = (
        secrets_store.get_secret(secrets_store.GMAIL_ADDRESS) or PLACEHOLDER_FROM_ADDRESS
    )

    try:
        message = build_email_message(action, from_address)
    except ValueError as error:
        return results.ExecutorResult.failed(
            ACTION_KIND, f"not sent — {error}",
            quote=action.quote, speaker=action.speaker, meeting_id=action.meeting_id,
        )
    preview = render_email_preview(message)

    # --- TRANSPORT SWAP ---
    if mode == results.MODE_LIVE:
        password = secrets_store.get_secret(secrets_store.GMAIL_APP_PASSWORD) or ""
        try:
            message_id = _send_message_live(message, from_address, password)
        except Exception as error:  # noqa: BLE001
            # smtplib can fail after the server has already accepted the message,
            # so this card is badged live: it must admit the mail may have gone.
            return results.ExecutorResult.failed(
                ACTION_KIND, f"SMTP send failed: {error}",
                mode=results.MODE_LIVE,
                quote=action.quote, speaker=action.speaker, meeting_id=action.meeting_id,
            )
        return results.ExecutorResult(
            ok=True,
            kind=ACTION_KIND,
            external_id=message_id,
            url=None,
            human_summary=f"Emailed {preview['to']} — {preview['subject']}",
            mode=results.MODE_LIVE,
            # Deliberately thin: there is nothing to undo with, and a fat
            # undo_payload would imply otherwise. is_undoable stays True only
            # because a record exists; undo() below refuses regardless.
            undo_payload={"message_id": message_id, "to": preview["to"], "irreversible": True},
            quote=action.quote,
            speaker=action.speaker,
            meeting_id=action.meeting_id,
        )

    return _render_simulated(action, preview)


def undo(result: results.ExecutorResult) -> bool:
    """Always False for a live send: email cannot be recalled. Implemented on purpose.

    A simulated send is trivially "undone" because nothing left the machine — say
    so honestly rather than claiming a capability that does not exist.
    """
    if result.mode == results.MODE_SIM:
        print("[email_send] simulated message was never sent — nothing to undo")
        return True
    print("[email_send] cannot undo a sent email — the regret window was the undo")
    return False


def describe_undo_capability(result: results.ExecutorResult) -> str:
    """One line for the board's undo button tooltip. The board should DISABLE the
    button for a live email rather than offering an action that will refuse."""
    if result.mode == results.MODE_SIM:
        return "Nothing was sent — discarding the simulated message."
    return "Email is irreversible — the 60-second countdown was the undo."


# --- TRANSPORT SWAP (bottom of the module) ----------------------------------


def _send_message_live(message: EmailMessage, from_address: str, app_password: str) -> str:
    """The only outbound send in this module. Returns the Message-ID.

    Past the send_message() line the action is permanent. Nothing below this
    function; nothing above it touches the network.
    """
    print(f"[{ACTION_KIND}] LIVE — sending to {message['To']} via {SMTP_HOST}")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(from_address, app_password)
        server.send_message(message)
    return message["Message-ID"]


PLACEHOLDER_ADDRESS_DOMAINS = ("example.com", "example.org", "example.net")


def names_a_placeholder_address(recipients: object) -> bool:
    """True when a recipient still points at a reserved example domain.

    The sim card IS the demo for this kind — "it built the exact payload, it just
    did not make the last call" — so an address that reads as a placeholder to
    anyone technical quietly undercuts the claim. Better to say so on the card
    than to let the room notice it first.
    """
    addresses = recipients if isinstance(recipients, (list, tuple)) else [recipients]
    return any(
        str(address).lower().endswith("@" + domain)
        for address in addresses
        for domain in PLACEHOLDER_ADDRESS_DOMAINS
    )


def _render_simulated(action: Action, preview: dict) -> results.ExecutorResult:
    """Sim mode: the exact email that would have been sent, unsent."""
    note = "(simulated)"
    if names_a_placeholder_address(preview.get("to")):
        # The disclosure stays — a placeholder address is exactly the thing a
        # viewer must be told about. The CONFIG VARIABLE does not: this string is
        # rendered on a card and in the ledger, and an env-var name there is
        # operator instruction leaking into product copy. The operator learns
        # what to set from the planner's log line and from Connections.
        note = "(simulated — no address on file)"
    return results.ExecutorResult.simulated(
        ACTION_KIND,
        f"Would email {preview['to']} — {preview['subject']} {note}",
        rendered_payload=preview,
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
    )
