"""
github_issues.py — GitHub Issues integration for the draft approval pipeline.
Creates issues with draft posts for review; reads approval comments; publishes approved posts.
"""
import logging
import re
from datetime import datetime, timezone

import requests

from config import GITHUB_TOKEN

log = logging.getLogger(__name__)

REPO = "ecastrom/social-bot"
API_BASE = "https://api.github.com"
LABEL = "draft-review"


def _headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _ensure_label_exists():
    """Create the draft-review label if it doesn't exist yet."""
    url = f"{API_BASE}/repos/{REPO}/labels"
    r = requests.get(url, headers=_headers(), timeout=10)
    r.raise_for_status()
    existing = {lbl["name"] for lbl in r.json()}
    if LABEL not in existing:
        requests.post(
            url,
            headers=_headers(),
            json={
                "name": LABEL,
                "color": "0E8A16",
                "description": "Draft posts awaiting approval",
            },
            timeout=10,
        ).raise_for_status()
        log.info(f"Created label '{LABEL}' in {REPO}.")


def create_approval_issue(drafts: list[dict]) -> dict:
    """
    Create a GitHub issue with draft posts for review.

    Args:
        drafts: List of dicts with keys: content, language, topic_tag, rationale.

    Returns:
        Dict with 'issue_number' and 'url'.
    """
    _ensure_label_exists()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = f"\U0001f4dd Draft posts for review \u2014 {today}"

    body_lines = [
        "## Draft posts for approval\n",
        "Review the drafts below. To approve, comment with one of:\n",
        "- `approve all` \u2014 approve every draft",
        "- `approve 1,3` \u2014 approve only drafts 1 and 3",
        "- `reject all` \u2014 reject everything and close the issue\n",
        "---\n",
    ]

    for i, draft in enumerate(drafts, start=1):
        lang_flag = "\U0001f1fa\U0001f1f8" if draft["language"] == "en" else "\U0001f1f2\U0001f1fd"
        body_lines.append(f"### Draft {i} {lang_flag} `{draft['topic_tag']}`\n")
        body_lines.append(f"> {draft['content']}\n")
        body_lines.append(f"**Rationale:** {draft['rationale']}\n")
        body_lines.append(f"**Characters:** {len(draft['content'])}/500\n")
        body_lines.append("---\n")

    body = "\n".join(body_lines)

    url = f"{API_BASE}/repos/{REPO}/issues"
    r = requests.post(
        url,
        headers=_headers(),
        json={
            "title": title,
            "body": body,
            "labels": [LABEL],
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    issue_number = data["number"]
    issue_url = data["html_url"]
    log.info(f"Created approval issue #{issue_number}: {issue_url}")
    return {"issue_number": issue_number, "url": issue_url}


def _parse_approval_command(comment_body: str) -> list[int] | str | None:
    """
    Parse an approval comment.

    Returns:
        - "all" if the comment says 'approve all'
        - "reject" if the comment says 'reject all'
        - list of ints if the comment says 'approve 1,3'
        - None if no valid command found
    """
    text = comment_body.strip().lower()

    if re.match(r"^approve\s+all\s*$", text):
        return "all"
    if re.match(r"^reject\s+all\s*$", text):
        return "reject"

    m = re.match(r"^approve\s+([\d,\s]+)\s*$", text)
    if m:
        nums = [int(n.strip()) for n in m.group(1).split(",") if n.strip().isdigit()]
        return nums if nums else None

    return None


def _extract_drafts_from_body(body: str) -> list[dict]:
    """
    Extract the draft posts from the issue body.

    Returns list of dicts with 'content', 'language', 'topic_tag'.
    """
    drafts = []
    # Match each draft block: ### Draft N [flag] `topic_tag`\n\n> content
    pattern = re.compile(
        r"### Draft \d+\s+.{1,4}\s+`([^`]+)`\s*\n\s*>\s*(.+?)(?:\n\n|\n\*\*)",
        re.DOTALL,
    )
    for match in pattern.finditer(body):
        topic_tag = match.group(1).strip()
        content = match.group(2).strip()
        # Detect language from flag in the header
        start = match.start()
        snippet = body[max(0, start - 5) : start + 50]
        language = "es" if "\U0001f1f2\U0001f1fd" in snippet or "\U0001f1f2\U0001f1fd" in body[start:start+80] else "en"
        drafts.append({
            "content": content,
            "language": language,
            "topic_tag": topic_tag,
        })
    return drafts


def check_approved_issues() -> list[dict]:
    """
    Check open issues with the draft-review label for approval comments.

    Returns:
        List of approved post dicts with keys: content, language, topic_tag.
        Each processed issue is closed after extraction.
    """
    # List open issues with the label
    url = f"{API_BASE}/repos/{REPO}/issues"
    params = {
        "labels": LABEL,
        "state": "open",
        "sort": "created",
        "direction": "asc",
    }
    r = requests.get(url, headers=_headers(), params=params, timeout=10)
    r.raise_for_status()
    issues = r.json()

    if not issues:
        log.info("No open draft-review issues found.")
        return []

    approved_posts = []

    for issue in issues:
        issue_number = issue["number"]
        issue_body = issue.get("body", "")
        log.info(f"Processing issue #{issue_number}...")

        # Get comments
        comments_url = f"{API_BASE}/repos/{REPO}/issues/{issue_number}/comments"
        cr = requests.get(comments_url, headers=_headers(), timeout=10)
        cr.raise_for_status()
        comments = cr.json()

        # Find the latest valid approval command
        command = None
        for comment in comments:
            parsed = _parse_approval_command(comment.get("body", ""))
            if parsed is not None:
                command = parsed

        if command is None:
            log.info(f"Issue #{issue_number}: no approval command yet, skipping.")
            continue

        # Extract drafts from the issue body
        drafts = _extract_drafts_from_body(issue_body)

        if command == "reject":
            log.info(f"Issue #{issue_number}: rejected. Closing.")
            _close_issue(issue_number, "Rejected by reviewer. No posts published.")
            continue

        if command == "all":
            approved_posts.extend(drafts)
            log.info(f"Issue #{issue_number}: approved all {len(drafts)} drafts.")
        elif isinstance(command, list):
            for idx in command:
                if 1 <= idx <= len(drafts):
                    approved_posts.append(drafts[idx - 1])
                else:
                    log.warning(f"Issue #{issue_number}: draft index {idx} out of range.")
            log.info(f"Issue #{issue_number}: approved drafts {command}.")

        _close_issue(issue_number, f"Processed. {len(approved_posts)} post(s) queued for publishing.")

    return approved_posts


def _close_issue(issue_number: int, comment: str):
    """Add a comment and close the issue."""
    # Add comment
    comment_url = f"{API_BASE}/repos/{REPO}/issues/{issue_number}/comments"
    requests.post(
        comment_url,
        headers=_headers(),
        json={"body": f"\u2705 {comment}"},
        timeout=10,
    )

    # Close issue
    issue_url = f"{API_BASE}/repos/{REPO}/issues/{issue_number}"
    requests.patch(
        issue_url,
        headers=_headers(),
        json={"state": "closed"},
        timeout=10,
    )
    log.info(f"Issue #{issue_number} closed.")
