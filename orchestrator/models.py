"""Explicit model selectors, not fuzzy matching or a hardcoded latest-version table."""

CLAUDE_FAMILIES = frozenset({"opus", "sonnet", "haiku", "fable"})


def model_family(model):
    """Recognize only documented Claude families; exact IDs retain their meaning."""
    if isinstance(model, str) and model.lower() in CLAUDE_FAMILIES:
        return model.lower()
    return None


def normalize_model(model):
    """Canonicalize family casing without modifying a version pin or repairing input."""
    return model_family(model) or model
