"""
content_generator.py — AI-powered content generation for Threads
Uses Claude to generate posts that reflect Edgar's intellectual voice.
"""
import logging
import json
from pathlib import Path

import anthropic

from config import ANTHROPIC_API_KEY

log = logging.getLogger(__name__)

PROFILE_PATH = Path(__file__).parent / "edgar_profile.md"

SYSTEM_PROMPT = """\
You are ghostwriting social media posts for Edgar Castro Mendez, an economist \
at Tecnologico de Monterrey. Edgar has a PhD from George Mason University \
(public choice, experimental economics, Austrian tradition). He is a classical \
liberal — skeptical of government intervention but intellectually honest about \
market failures. He is going on the academic job market soon.

Your job is to write SHORT posts for Threads (max 500 characters each). These \
posts should sound like Edgar thinking out loud — a real person with real opinions, \
not a corporate account or an AI-generated motivational poster.

VOICE GUIDELINES:
- Casual but rigorous. Like a sharp economist thinking out loud with smart friends.
- Ground every point in a concrete example, a real case, or an empirical finding.
  Don't state a conclusion — show the mechanism or the data that leads to it.
- Provocative in the "hmm, I never thought of it that way" sense — not in a
  "the left/right is wrong again" sense.
- Can be funny or wry, but substance comes first.
- NO politicians by name. No partisan framing. If a policy is interesting, explain
  WHY it produces a certain outcome — not who supports or opposes it.
- NO ideological slogans or crusading (avoid phrases like "statism", "big government",
  "free market wins again"). Let the example do the persuading.
- NO hashtags. NO emojis (or at most one, sparingly). NO "thread" or "let me explain" openings.
- Never start with "As an economist" or similar self-referential framing.
- Do not sound like a LinkedIn post or a TED talk promo.

LANGUAGE RULES:
- Write in SPANISH when the topic is about Latin America, Mexican policy, \
LATAM economics, or local concerns. Use natural Latin American Spanish (Mexican register).
- Write in ENGLISH when the topic is about international economics, academic life, \
the job market, or general ideas that travel across borders.
- Each post must be in ONE language only. No code-switching within a post.

CONTENT PRIORITIES:
- Counterintuitive empirical findings from economics or political science
- Real historical or policy cases where outcomes surprised everyone — explain why
- What lab and field experiments reveal about how people actually make decisions
- Specific mechanisms: why a particular policy produces a particular result
- Institutional puzzles: why some places can run good policy and others can't
- AI and technology changing how things work in practice
- Academic life and job market observations (light touch, occasional)

Return your output as a JSON array. Each element must have exactly these fields:
- "content": the post text (max 500 chars, hard limit)
- "language": "en" or "es"
- "topic_tag": a short label (2-4 words, lowercase, e.g. "price controls", "job market")
- "rationale": one sentence explaining why this post is worth publishing
"""


def _load_profile() -> str:
    """Load Edgar's intellectual profile from the markdown file."""
    if PROFILE_PATH.exists():
        return PROFILE_PATH.read_text(encoding="utf-8")
    log.warning(f"Profile not found at {PROFILE_PATH}; generating without it.")
    return ""


def generate_post_drafts(topics: list[str] | None = None, num_posts: int = 2) -> list[dict]:
    """
    Generate draft posts using Claude.

    Args:
        topics: Optional list of topic hints to guide generation.
        num_posts: Number of posts to generate (default 2).

    Returns:
        List of dicts with keys: content, language, topic_tag, rationale.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    profile_text = _load_profile()

    user_message = f"Generate exactly {num_posts} Threads posts."
    if topics:
        user_message += f"\n\nFocus on these topics: {', '.join(topics)}"
    if profile_text:
        user_message += f"\n\nHere is Edgar's full intellectual profile for context:\n\n{profile_text}"
    user_message += (
        "\n\nReturn ONLY a JSON array — no markdown fences, no commentary. "
        "Each post must be under 500 characters."
    )

    log.info(f"Requesting {num_posts} draft posts from Claude...")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw_text = response.content[0].text.strip()

    # Strip markdown fences if the model wrapped them anyway
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        # Remove first line (```json) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw_text = "\n".join(lines)

    try:
        drafts = json.loads(raw_text)
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Claude response as JSON: {e}")
        log.error(f"Raw response: {raw_text[:500]}")
        raise ValueError(f"Claude returned invalid JSON: {e}") from e

    if not isinstance(drafts, list):
        raise ValueError(f"Expected a JSON array, got {type(drafts).__name__}")

    # Validate and sanitize each draft
    validated = []
    required_keys = {"content", "language", "topic_tag", "rationale"}
    for i, draft in enumerate(drafts):
        missing = required_keys - set(draft.keys())
        if missing:
            log.warning(f"Draft {i} missing keys {missing}, skipping.")
            continue

        # Enforce character limit
        if len(draft["content"]) > 500:
            log.warning(
                f"Draft {i} is {len(draft['content'])} chars, truncating to 500."
            )
            draft["content"] = draft["content"][:497] + "..."

        # Normalize language tag
        draft["language"] = draft["language"].lower().strip()
        if draft["language"] not in ("en", "es"):
            draft["language"] = "en"

        validated.append({
            "content": draft["content"],
            "language": draft["language"],
            "topic_tag": draft["topic_tag"],
            "rationale": draft["rationale"],
        })

    log.info(f"Generated {len(validated)} valid drafts.")
    return validated
