"""Distinguish a native coordinator from a worker managed by another program."""

import os


def externally_managed() -> bool:
    """Do not attach coordinator hooks to supervised workers or validation agents.

    These markers select integration behavior, not approval authority.
    Skipping a hook creates no session, project binding, or worker grant.
    Native harness permissions and the external runner's own rules still apply.
    """
    return os.environ.get("ORCHESTRATOR_CHILD") == "1" or bool(os.environ.get("NO_MISTAKES_GATE"))
