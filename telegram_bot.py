"""
telegram_bot.py — Telegram webhook server for the Threads content bot.

Edgar sends a link + rough thought via Telegram. The bot generates 2 draft
posts with Claude, sends them back, and handles approve/revise/discard
until Edgar is happy — then publishes directly to Threads.

Conversation states:
  idle               → waiting for a new thought/link
  awaiting_decision  → sent 2 drafts, waiting for 1/2/revise/discard
  awaiting_revision  → sent revised draft, waiting for approve/revise/discard

Deploy on Render (free tier). Set Telegram webhook to:
  https://<render-app>.onrender.com/<TELEGRAM_BOT_TOKEN>
"""
import os
import re
import logging

import telebot
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv(override=True)

from content_generator import generate_from_input
from threads_client import ThreadsClient
from config import load_threads_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
EDGAR_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]  # numeric string, e.g. "123456789"

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown", threaded=False)
app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory conversation state (single user — keyed by chat_id string)
# ---------------------------------------------------------------------------

conversations: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r'https?://\S+')

def extract_url(text: str) -> tuple[str | None, str]:
    m = _URL_RE.search(text)
    if not m:
        return None, text.strip()
    url = m.group(0)
    note = (text[:m.start()] + text[m.end():]).strip()
    return url, note

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_drafts(drafts: list[dict]) -> str:
    lines = []
    for i, d in enumerate(drafts, 1):
        lang = d["language"].upper()
        chars = len(d["content"])
        lines.append(f"*Draft {i}* ({lang} · {chars} chars)\n{d['content']}")
    lines.append(
        "\nReply:\n"
        "• *1* or *2* — approve and post to Threads\n"
        "• *revise 1: your notes* — adjust a draft\n"
        "• *discard* — drop both and start over"
    )
    return "\n\n".join(lines)

# ---------------------------------------------------------------------------
# State handlers
# ---------------------------------------------------------------------------

def handle_idle(chat_id: str, body: str):
    url, note = extract_url(body)

    if not note and not url:
        bot.send_message(chat_id, "Send me a link, a rough thought, or both — I'll draft a Threads post for you.")
        return

    if not note:
        note = "This article seems worth sharing."

    bot.send_message(chat_id, "On it — generating drafts...")

    try:
        drafts = generate_from_input(note=note, url=url, num_posts=2)
    except Exception as e:
        log.error(f"Generation failed: {e}")
        bot.send_message(chat_id, f"Generation failed: {e}\n\nTry again.")
        return

    if not drafts:
        bot.send_message(chat_id, "Couldn't generate drafts. Try rephrasing your thought.")
        return

    conversations[chat_id] = {
        "status": "awaiting_decision",
        "drafts": drafts,
        "original_note": note,
        "original_url": url,
    }

    bot.send_message(chat_id, format_drafts(drafts))


def handle_awaiting_decision(chat_id: str, body: str, state: dict):
    text = body.strip().lower()
    drafts = state["drafts"]

    # Approve 1 or 2
    m = re.match(r'^(approve\s+)?([12])$', text)
    if m:
        idx = int(m.group(2)) - 1
        _publish(chat_id, drafts[idx])
        return

    # Revise a specific draft
    m = re.match(r'^revise\s+([12])\s*:\s*(.+)$', text, re.DOTALL)
    if m:
        idx = int(m.group(1)) - 1
        notes = m.group(2).strip()
        _revise(chat_id, drafts[idx], notes, state)
        return

    # Discard
    if text in ("discard", "reject", "cancel", "no"):
        conversations.pop(chat_id, None)
        bot.send_message(chat_id, "Discarded. Send a new thought whenever you're ready.")
        return

    # If it's a long message, treat it as a full replacement draft
    if len(body.strip()) > 50:
        _revise(chat_id, drafts[0], body.strip(), state)
        return

    bot.send_message(chat_id,
        "Reply:\n"
        "• *1* or *2* to approve and post\n"
        "• *revise 1: your notes* to adjust\n"
        "• Paste your own version to use it as the draft\n"
        "• *discard* to start over"
    )


def handle_awaiting_revision(chat_id: str, body: str, state: dict):
    text = body.strip().lower()
    draft = state["current_draft"]

    # Approve
    if re.match(r'^(approve|ok|yes|post|post it|looks good|perfect|dale)$', text):
        _publish(chat_id, draft)
        return

    # Explicit revise command
    m = re.match(r'^revise\s*:\s*(.+)$', text, re.DOTALL)
    if m:
        _revise(chat_id, draft, m.group(1).strip(), state)
        return

    # Free-form revision notes (longer than 15 chars — clearly not a command)
    if text not in ("discard", "reject", "cancel", "no") and len(body.strip()) > 15:
        _revise(chat_id, draft, body.strip(), state)
        return

    # Discard
    if text in ("discard", "reject", "cancel", "no"):
        conversations.pop(chat_id, None)
        bot.send_message(chat_id, "Discarded. Send a new thought whenever you're ready.")
        return

    bot.send_message(chat_id,
        "Reply:\n"
        "• *approve* — post this to Threads\n"
        "• *revise: your notes* — adjust further\n"
        "• *discard* — drop it"
    )

# ---------------------------------------------------------------------------
# Core actions
# ---------------------------------------------------------------------------

def _revise(chat_id: str, draft: dict, notes: str, state: dict):
    # If the "notes" look like a complete draft (>100 chars and similar length
    # to the original), treat them AS the new draft — just polish minimally.
    notes_look_like_full_draft = len(notes) > 100

    if notes_look_like_full_draft:
        bot.send_message(chat_id, "Polishing your version...")
        revision_prompt = (
            f"The user wrote this as their preferred draft for a Threads post:\n\n"
            f"{notes}\n\n"
            f"Polish it MINIMALLY — fix only typos, grammar, or obvious wording issues. "
            f"Do NOT change the substance, structure, or tone. Keep it as close to "
            f"the user's version as possible. If it's already good, return it unchanged."
        )
    else:
        bot.send_message(chat_id, "Revising...")
        revision_prompt = (
            f"Current draft:\n{draft['content']}\n\n"
            f"Revision notes from the author: {notes}\n\n"
            f"Apply ONLY the specific changes requested. Keep everything else "
            f"exactly as it was. Do not rewrite from scratch."
        )

    try:
        new_drafts = generate_from_input(note=revision_prompt, url=None, num_posts=1)
    except Exception as e:
        bot.send_message(chat_id, f"Revision failed: {e}\n\nTry again.")
        return

    if not new_drafts:
        bot.send_message(chat_id, "Couldn't revise. Try rephrasing your notes.")
        return

    revised = new_drafts[0]
    conversations[chat_id] = {
        "status": "awaiting_revision",
        "current_draft": revised,
        "original_note": state.get("original_note", ""),
        "original_url": state.get("original_url"),
    }

    bot.send_message(chat_id,
        f"*Revised* ({len(revised['content'])} chars)\n\n"
        f"{revised['content']}\n\n"
        "Reply *approve* to post, or keep sending revision notes, or *discard*."
    )


def _publish(chat_id: str, draft: dict):
    bot.send_message(chat_id, "Posting to Threads...")
    try:
        threads = ThreadsClient(load_threads_config())
        threads.post(draft["content"])
        conversations.pop(chat_id, None)
        bot.send_message(chat_id, f"Posted ✓\n\n{draft['content']}", parse_mode=None)
        log.info(f"Published to Threads: {draft['content'][:60]}...")
    except Exception as e:
        log.error(f"Publish failed: {e}")
        bot.send_message(chat_id, f"Failed to post to Threads: {e}", parse_mode=None)

# ---------------------------------------------------------------------------
# Telegram message handler
# ---------------------------------------------------------------------------

@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_message(message):
    chat_id = str(message.chat.id)

    # Security: only respond to Edgar
    if chat_id != EDGAR_CHAT_ID:
        log.warning(f"REJECTED chat_id={chat_id!r} (expected {EDGAR_CHAT_ID!r})")
        return

    body = (message.text or "").strip()
    if not body:
        return

    log.info(f"Message from chat_id={chat_id} (expected={EDGAR_CHAT_ID}): {body[:80]}")

    state = conversations.get(chat_id, {})
    status = state.get("status", "idle")

    try:
        print(f"[HANDLER] status={status}, body={body[:80]}", flush=True)
        if status == "idle":
            handle_idle(chat_id, body)
        elif status == "awaiting_decision":
            handle_awaiting_decision(chat_id, body, state)
        elif status == "awaiting_revision":
            handle_awaiting_revision(chat_id, body, state)
        print(f"[HANDLER] completed OK", flush=True)
    except Exception as e:
        print(f"[HANDLER] ERROR: {e}", flush=True)
        log.error(f"Unhandled error: {e}", exc_info=True)
        bot.send_message(chat_id, "Something went wrong. Send a new message to start over.")
        conversations.pop(chat_id, None)

# ---------------------------------------------------------------------------
# Flask app (webhook receiver)
# ---------------------------------------------------------------------------

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    raw = request.data.decode("utf-8")
    print(f"[WEBHOOK] Raw update: {raw[:500]}", flush=True)
    update = telebot.types.Update.de_json(raw)
    print(f"[WEBHOOK] Update parsed: message={update.message}, edited={update.edited_message}", flush=True)
    bot.process_new_updates([update])
    return "ok", 200


@app.route("/health")
def health():
    return "ok", 200


if __name__ == "__main__":
    # Local development: use polling instead of webhook
    log.info("Starting in polling mode (local dev)...")
    bot.remove_webhook()
    bot.infinity_polling()
