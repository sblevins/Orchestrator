"""Conservative prompt triage without delegating reviewer visibility to the coordinator."""

from __future__ import annotations

import re

ROUTINE_PROMPTS = {
    "/status",
    "/tasks",
    "/progress",
    "status",
    "progress",
    "what's the status",
    "what is the status",
    "what's the status of work",
    "what is the status of work",
    "how is it going",
    "show progress",
    "show the current status",
    "list running tasks",
}


def needs_review(prompt: str, configuration: dict | None = None) -> bool:
    """Only exact known read-only queries skip a wake; ambiguity is review-worthy."""
    settings = (configuration or {}).get("monitoring", {})
    if settings.get("review_every_prompt", False):
        return True
    normalized = re.sub(r"\s+", " ", prompt.strip().lower()).rstrip("?.!").strip()
    allowed = settings.get("routine_prompts", sorted(ROUTINE_PROMPTS))
    return normalized not in allowed
