"""Explicit model selectors, not fuzzy matching or a hardcoded latest-version table."""

PI_FAMILIES = frozenset({"astra", "sol"})


CLAUDE_FAMILIES = frozenset({"opus", "sonnet", "haiku", "fable"})


def model_family(model):
    """Recognize only documented Claude families; exact IDs retain their meaning."""
    if isinstance(model, str) and model.lower() in CLAUDE_FAMILIES:
        return model.lower()
    return None


def selector_family(model):
    """Family selectors across harnesses; do not use this to infer Anthropic ownership."""
    return model_family(model) or (
        model.lower() if isinstance(model, str) and model.lower() in PI_FAMILIES else None
    )


def normalize_model(model):
    """Canonicalize family casing without modifying a version pin or repairing input."""
    return selector_family(model) or model
