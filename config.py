"""
config.py — Central configuration
Loads environment variables for Threads, Anthropic, and GitHub APIs.
"""
import os
from dataclasses import dataclass


@dataclass
class ThreadsConfig:
    app_id: str
    app_secret: str
    access_token: str
    user_id: str  # Threads numeric user ID


def load_threads_config() -> ThreadsConfig:
    return ThreadsConfig(
        app_id=os.environ["THREADS_APP_ID"],
        app_secret=os.environ["THREADS_APP_SECRET"],
        access_token=os.environ["THREADS_ACCESS_TOKEN"],
        user_id=os.environ["THREADS_USER_ID"],
    )


# Module-level constants for simple env vars (no dataclass needed)
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN: str = os.environ.get("GITHUB_TOKEN", "")
