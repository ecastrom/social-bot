"""
content_generator.py — AI-powered content generation for Threads
Uses Claude to generate posts that reflect Edgar's intellectual voice.

Two modes:
  generate_from_input(url, note)   — Edgar provides a link + raw reflection (primary)
  generate_post_drafts(topics)     — Fully autonomous generation (scheduled fallback)
"""
import logging
import json
import re
import html
from pathlib import Path

import anthropic
import requests as http_requests

from config import ANTHROPIC_API_KEY

log = logging.getLogger(__name__)

PROFILE_PATH = Path(__file__).parent / "edgar_profile.md"

# ---------------------------------------------------------------------------
# Shared voice guidelines (used in both prompts)
# ---------------------------------------------------------------------------

_VOICE = """\
VOICE GUIDELINES:
- Casual but rigorous. Like a sharp economist thinking out loud with smart friends.
- Ground every point in a concrete example, a real case, or an empirical finding.
  Don't state a conclusion — show the mechanism or the data that leads to it.
- Can be funny or wry, but substance comes first.
- NO hashtags. NO emojis (or at most one, sparingly). NO "thread" or "let me explain" openings.
- Never start with "As an economist" or similar self-referential framing.
- Do not sound like a LinkedIn post or a TED talk promo.

NEVER HEDGE OR SOFTEN. Banned phrases: "it could be argued", "many would say",
"arguably", "some might suggest", "podría argumentarse", "algunos dirían",
"es importante destacar", "cabe señalar", "resulta evidente", "es fundamental".

BANNED RHETORICAL STRUCTURES — if you catch yourself writing any of these, rewrite:
  Spanish: "no es X: es Y", "no es solo X", "es algo más que X", "más allá de X",
  "va más allá", "en este contexto", "complejo entramado", "en última instancia",
  "en definitiva", "no podemos ignorar", "la tensión entre X e Y" (as vague filler),
  "nos invita a reflexionar", "vale la pena preguntarse", "abre la puerta a".
  English: "It's not just X, it's Y", "More than just", "At its core", "The real
  question is", "What we're really talking about", "Here's the thing:", "The bottom
  line is", "In a very real sense", "Ultimately,", "Crucially,", "Importantly,",
  "It's worth noting that", "This matters because", "The key insight here is",
  "That said,", "To be clear,", "Needless to say,", "Of course,", "Interestingly,",
  "Strikingly,", "Tellingly,", "Notably,", "Fascinatingly,".

BANNED VOCABULARY — vague filler that makes writing feel synthetic:
  Spanish: "paradigma", "disruptivo", "holístico", "sistémico" (as vague adjective),
  "robusto" (for anything strong), "narrativa" (used loosely), "ecosistema" (metaphorical),
  "desafíos" and "oportunidades" (without specifics), "complejidad" as a conclusion,
  "a nivel de", "en el marco de", "desde la perspectiva de", "en términos de".
  English: "landscape" (the X landscape), "ecosystem" (metaphorical), "paradigm shift",
  "robust", "nuanced" (especially as self-praise), "framework", "stakeholders",
  "unpack", "delve", "game-changer", "holistic", "at the end of the day",
  "deep dive", "leverage" (as verb), "moving forward".

BANNED OPENERS: Rhetorical questions ("¿Qué tienen en común X e Y?").
  Think-piece setups ("En 1994, algo curioso sucedió...").

BANNED CLOSINGS: "The lesson here is...", "What this tells us is...", "The takeaway
  is...", "The implications are significant", "¿Qué opinan?", "What do you think?"
  Do NOT end with an engagement-bait question.

NO EM-DASH ABUSE. At most one em-dash per post. If a draft has two or more, rewrite.

NEVER replace concrete examples with abstract summaries. Specific names, countries,
events, and numbers are always better than vague generalizations.

LANGUAGE RULES:
- Write in SPANISH when the topic is about Latin America, Mexican policy,
  LATAM economics, or local concerns. Use natural Latin American Spanish (Mexican register).
- Write in ENGLISH when the topic is about international economics, academic life,
  the job market, or general ideas that travel across borders.
- Each post must be in ONE language only. No code-switching within a post.

OUTPUT FORMAT — return ONLY a JSON array, no markdown fences, no commentary.
Each element must have exactly these fields:
- "content": the post text (max 500 chars, hard limit)
- "thread_part2": second post if content needed to be split into a thread (or null)
- "language": "en" or "es"
- "topic_tag": a short label (2-4 words, lowercase)
- "rationale": one sentence explaining why this post is worth publishing
"""

# ---------------------------------------------------------------------------
# Prompt for input-driven mode (primary)
# ---------------------------------------------------------------------------

INPUT_SYSTEM_PROMPT = """\
You are ghostwriting Threads posts for Edgar Castro Mendez, an economist at \
Tecnologico de Monterrey (PhD from George Mason — public choice, experimental \
economics). He is going on the academic job market soon.

Your job is to help Edgar say what he's already thinking — more precisely and \
with more supporting evidence, but NEVER more safely and NEVER more abstractly.

You will be told which mode applies based on input length:

COMPLETE INPUT mode (>250 chars): Edgar has already written the post.
  1. PRESERVE his structure, wording, examples, and conclusions exactly.
     Fix spelling and accents. Sharpen attributions (full names for quoted people).
     Do NOT restructure, summarize, or replace his concrete details.
  2. ENRICH (if you can do it without displacing his content): add one specific
     example, empirical finding, historical case, or reference that strengthens
     his point. Weave it in naturally.
  If the result exceeds 500 chars, split into a thread using "thread_part2".

THREAD mode (>500 chars): Edgar's input is already longer than the 500-char limit.
  Split into exactly 2 parts. Each part ≤ 500 chars, complete on its own.
  Do NOT summarize — preserve every specific example, name, and the conclusion.
  Use "thread_part2" for the second post.

FRAGMENT mode (<250 chars): Edgar gave a rough thought or fragment.
  Complete it in his voice using specific evidence — name the mechanism,
  find the case, cite the finding. Do not drift into generic commentary.

Failure modes to avoid in ALL modes:
- Replacing his concrete examples with abstract generalizations
- Adding qualifiers he didn't write ("podría argumentarse", "algunos dirían")
- Restructuring a complete thought because you think yours is better
- Using banned rhetorical structures as shortcuts

""" + _VOICE

# ---------------------------------------------------------------------------
# Prompt for autonomous mode (scheduled fallback)
# ---------------------------------------------------------------------------

AUTO_SYSTEM_PROMPT = """\
You are ghostwriting Threads posts for Edgar Castro Mendez, an economist at \
Tecnologico de Monterrey (PhD from George Mason — public choice, experimental \
economics). He is going on the academic job market soon.

Generate posts grounded in specific cases, empirical findings, or concrete \
mechanisms — not general commentary. Each post should feel like a real observation, \
not a think-piece opener.

CONTENT PRIORITIES:
- Counterintuitive empirical findings from economics or political science
- Real historical or policy cases where outcomes surprised everyone — explain why
- What lab and field experiments reveal about how people actually make decisions
- Specific mechanisms: why a particular policy produces a particular result
- Institutional puzzles: why some places can run good policy and others can't
- AI and technology changing how things work in practice

""" + _VOICE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_profile() -> str:
    if PROFILE_PATH.exists():
        return PROFILE_PATH.read_text(encoding="utf-8")
    log.warning(f"Profile not found at {PROFILE_PATH}; generating without it.")
    return ""


def _fetch_article(url: str, max_chars: int = 4000) -> str:
    """
    Fetch a URL and return a plain-text excerpt of the article body.
    Uses basic HTML stripping — no extra dependencies needed.
    Returns empty string on failure (non-blocking).
    """
    try:
        resp = http_requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        raw_html = resp.text

        # Remove script/style blocks
        raw_html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
        # Strip all tags
        text = re.sub(r"<[^>]+>", " ", raw_html)
        # Decode HTML entities
        text = html.unescape(text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        log.info(f"Fetched article: {len(text)} chars from {url}")
        return text
    except Exception as e:
        log.warning(f"Could not fetch article at {url}: {e}")
        return ""


def _parse_and_validate(raw_text: str) -> list[dict]:
    """Parse Claude's JSON output and validate each draft."""
    if raw_text.startswith("```"):
        lines = [l for l in raw_text.split("\n") if not l.strip().startswith("```")]
        raw_text = "\n".join(lines)

    try:
        drafts = json.loads(raw_text)
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Claude response as JSON: {e}")
        log.error(f"Raw response: {raw_text[:500]}")
        raise ValueError(f"Claude returned invalid JSON: {e}") from e

    if not isinstance(drafts, list):
        raise ValueError(f"Expected a JSON array, got {type(drafts).__name__}")

    validated = []
    required_keys = {"content", "language", "topic_tag", "rationale"}
    for i, draft in enumerate(drafts):
        missing = required_keys - set(draft.keys())
        if missing:
            log.warning(f"Draft {i} missing keys {missing}, skipping.")
            continue
        if len(draft["content"]) > 500:
            log.warning(f"Draft {i} is {len(draft['content'])} chars, truncating.")
            draft["content"] = draft["content"][:497] + "..."
        draft["language"] = draft["language"].lower().strip()
        if draft["language"] not in ("en", "es"):
            draft["language"] = "en"
        validated.append({
            "content": draft["content"],
            "thread_part2": draft.get("thread_part2") or None,
            "language": draft["language"],
            "topic_tag": draft["topic_tag"],
            "rationale": draft["rationale"],
        })

    log.info(f"Generated {len(validated)} valid drafts.")
    return validated


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_from_input(note: str, url: str | None = None, num_posts: int = 2) -> list[dict]:
    """
    Primary mode: generate posts from Edgar's raw input.

    Args:
        note: Edgar's raw reflection — a rough thought, reaction, or insight.
        url:  Optional URL of an article or source he is reacting to.
        num_posts: How many post variations to generate (default 2, pick the best one).

    Returns:
        List of draft dicts.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    profile_text = _load_profile()

    parts = []

    if url:
        article_text = _fetch_article(url)
        if article_text:
            parts.append(f"ARTICLE ({url}):\n{article_text}")
        else:
            parts.append(f"SOURCE URL (could not fetch content): {url}")

    parts.append(f"EDGAR'S RAW THOUGHT:\n{note.strip()}")

    # Inject mode instruction based on input length
    note_len = len(note.strip())
    if note_len > 500:
        mode_instruction = (
            "MODE: THREAD — Edgar's input exceeds 500 chars. "
            "Split into 2 parts using 'thread_part2'. Each ≤ 500 chars. "
            "Preserve every specific name, example, and the conclusion. Do NOT summarize."
        )
    elif note_len > 250:
        mode_instruction = (
            "MODE: COMPLETE INPUT — Edgar has already written the post. "
            "Preserve his structure and wording. Fix spelling/accents. "
            "Enrich with one specific addition if you can do it without displacing his content. "
            "Use 'thread_part2' if the result exceeds 500 chars."
        )
    else:
        mode_instruction = (
            "MODE: FRAGMENT — Complete Edgar's rough thought in his voice. "
            "Add specific examples, cases, or evidence. Stay on his specific angle."
        )
    parts.append(mode_instruction)

    if profile_text:
        parts.append(f"EDGAR'S INTELLECTUAL PROFILE (for voice/context):\n{profile_text}")

    parts.append(
        f"\nGenerate {num_posts} alternative post(s) based on Edgar's thought above. "
        "Return ONLY a JSON array, no markdown fences."
    )

    user_message = "\n\n".join(parts)

    log.info(f"Generating {num_posts} input-driven draft(s)...")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        system=INPUT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    return _parse_and_validate(response.content[0].text.strip())


def generate_post_drafts(topics: list[str] | None = None, num_posts: int = 2) -> list[dict]:
    """
    Fallback mode: autonomous generation for scheduled runs with no input.

    Args:
        topics: Optional topic hints.
        num_posts: Number of posts to generate.

    Returns:
        List of draft dicts.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    profile_text = _load_profile()

    parts = [f"Generate exactly {num_posts} Threads posts."]
    if topics:
        parts.append(f"Focus on these topics: {', '.join(topics)}")
    if profile_text:
        parts.append(f"Edgar's intellectual profile:\n{profile_text}")
    parts.append("Return ONLY a JSON array — no markdown fences, no commentary.")

    user_message = "\n\n".join(parts)

    log.info(f"Requesting {num_posts} autonomous draft posts from Claude...")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        system=AUTO_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    return _parse_and_validate(response.content[0].text.strip())
