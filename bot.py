"""
bot.py — Main orchestrator for the Threads content bot.

Usage:
  python bot.py generate          Generate draft posts and open a GitHub issue for review
  python bot.py publish           Check approved issues and publish to Threads
  python bot.py metrics           Show Threads metrics summary
  python bot.py list              List scheduled/published posts
  python bot.py help              Show this help
"""
import sys
import logging
import json
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(override=True)

import database as db
from config import load_threads_config
from threads_client import ThreadsClient
from content_generator import generate_post_drafts
from github_issues import create_approval_issue, check_approved_issues

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


class SocialBot:
    def __init__(self, need_threads: bool = True):
        db.init_db()
        if need_threads:
            self.threads = ThreadsClient(load_threads_config())
        else:
            self.threads = None

    # -- Generate drafts + create approval issue ----------------------------

    def generate(self, topics: list[str] | None = None, num_posts: int = 2):
        """Generate draft posts and create a GitHub issue for review."""
        log.info("Generating draft posts...")
        drafts = generate_post_drafts(topics=topics, num_posts=num_posts)

        if not drafts:
            log.warning("No drafts generated. Aborting.")
            return

        log.info(f"Generated {len(drafts)} drafts. Creating GitHub issue...")
        result = create_approval_issue(drafts)
        log.info(f"Review issue created: {result['url']}")
        print(f"\nDrafts ready for review: {result['url']}")

    # -- Publish approved posts ---------------------------------------------

    def publish(self):
        """Check for approved issues and publish approved posts to Threads."""
        log.info("Checking for approved posts...")
        approved = check_approved_issues()

        if not approved:
            log.info("No approved posts to publish.")
            print("No approved posts found.")
            return

        log.info(f"Found {len(approved)} approved posts. Publishing to Threads...")
        published = 0
        for post in approved:
            try:
                result = self.threads.post(post["content"])
                # Save to database for metrics tracking
                now = datetime.now(timezone.utc).isoformat()
                post_id = db.add_scheduled_post("threads", post["content"], now)
                db.mark_post_sent(post_id, {"threads": result})
                log.info(f"Published: {post['content'][:60]}...")
                published += 1
            except Exception as e:
                log.error(f"Failed to publish post: {e}")
                # Save failure record
                now = datetime.now(timezone.utc).isoformat()
                post_id = db.add_scheduled_post("threads", post["content"], now)
                db.mark_post_failed(post_id, str(e))

        print(f"\nPublished {published}/{len(approved)} posts to Threads.")

    # -- Collect metrics ----------------------------------------------------

    def collect_metrics(self):
        """Collect metrics for recently published posts."""
        with db.get_conn() as conn:
            recent = conn.execute(
                """SELECT id, platform, response FROM scheduled_posts
                   WHERE status='sent' AND created_at >= datetime('now', '-7 days')"""
            ).fetchall()

        updated = 0
        for row in recent:
            response = json.loads(row["response"] or "{}")
            post_id = response.get("threads", {}).get("id")
            if post_id:
                m = self.threads.get_post_metrics(post_id)
                if m:
                    db.save_metric("threads", post_id, m["likes"], m["reposts"], m["replies"], m["impressions"])
                    updated += 1

        log.info(f"Metrics updated for {updated} posts.")

    # -- Display metrics ----------------------------------------------------

    def show_metrics(self):
        """Show a summary of Threads metrics."""
        self.collect_metrics()
        summary = db.get_metrics_summary(platform="threads")
        if not summary:
            print("No metrics recorded yet.")
            return
        data = summary.get("threads", {})
        print("\nTHREADS METRICS SUMMARY")
        print("-" * 35)
        print(f"  Likes:       {data.get('likes', 0):,}")
        print(f"  Reposts:     {data.get('reposts', 0):,}")
        print(f"  Replies:     {data.get('replies', 0):,}")
        print(f"  Impressions: {data.get('impressions', 0):,}")

    # -- List posts ---------------------------------------------------------

    def list_posts(self):
        """List all scheduled/published posts."""
        posts = db.list_scheduled_posts()
        if not posts:
            print("No posts in the database.")
            return
        print(f"\nPOSTS ({len(posts)} total)")
        print("-" * 60)
        for p in posts:
            status_icon = {"pending": "[PENDING]", "sent": "[SENT]", "failed": "[FAILED]"}.get(p["status"], "[?]")
            print(f"\n{status_icon} #{p['id']} | {p['scheduled_at']}")
            print(f"  {p['content'][:80]}{'...' if len(p['content']) > 80 else ''}")


# -- CLI -------------------------------------------------------------------

def print_help():
    print("""
Threads Content Bot -- Commands:

  python bot.py generate [topic1 topic2 ...]
      Generate draft posts and create a GitHub issue for review.
      Optional: provide topic hints as arguments.

  python bot.py publish
      Check approved GitHub issues and publish to Threads.

  python bot.py metrics
      Collect and display Threads metrics.

  python bot.py list
      List all posts in the database.

  python bot.py help
      Show this help.
""")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "generate":
        topics = sys.argv[2:] if len(sys.argv) > 2 else None
        bot = SocialBot(need_threads=False)
        bot.generate(topics=topics)

    elif cmd == "publish":
        bot = SocialBot(need_threads=True)
        bot.publish()

    elif cmd == "metrics":
        bot = SocialBot(need_threads=True)
        bot.show_metrics()

    elif cmd == "list":
        db.init_db()
        bot = SocialBot.__new__(SocialBot)
        bot.list_posts()

    else:
        print_help()
