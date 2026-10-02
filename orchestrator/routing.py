"""Pure policy validation and selection gates, without a routing model.

The caller chooses the best-fitting natural-language rule. This module never
interprets task prose or changes configured executors or models.
Quota-dependent selections remain blocked until a supported evidence adapter is
available: an arbitrary dictionary of model-reported numbers is not evidence.
"""

import hashlib
import json
import math
import os
import re
import stat
from copy import deepcopy
from pathlib import Path

from .models import model_family, normalize_model


class RoutingError(ValueError):
    """A policy or selection needs an explicit configuration/operator action."""


_PROVIDER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_POLICY_EFFORTS = {
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "max-supported",
    "ultra",
}
_EXECUTOR_EFFORTS = {
    "pi": {"off", "minimal", "low", "medium", "high", "xhigh", "max", "max-supported"},
    "claude": {"low", "medium", "high", "xhigh", "max"},
}
_MAX_POLICY_BYTES = 1024 * 1024
_PROFILE_KEYS = ("harness", "model", "provider", "effort", "floor")
_CONFIRMATIONS = {"user", "captain"}
_DEFAULT_DIFFICULTY_LEVELS = {
    "easy": {"claude": "low", "pi": "low"},
    "hard": {"claude": "high", "pi": "high"},
    "very-hard": {"claude": "max", "pi": "max-supported"},
}


def _difficulty(value, context):
    _text(value, context)
    normalized = re.sub(r"[\s_-]+", "-", value.strip().lower())
    if normalized not in _DEFAULT_DIFFICULTY_LEVELS:
        raise RoutingError(f"{context} must be easy, hard, or very-hard")
    return normalized


def _effort(profile, choice, policy, fallback_difficulty=None):
    if "effort" in choice and "difficulty" in choice:
        raise RoutingError("choice.effort and choice.difficulty are alternatives; supply only one")
    difficulty = choice.get("difficulty")
    if difficulty is None and "effort" not in choice and "effort" not in profile:
        difficulty = fallback_difficulty
    if difficulty is not None:
        difficulty = _difficulty(difficulty, "choice.difficulty")
        harness = profile["harness"]
        return (
            policy.get("difficulty_levels", {})
            .get(difficulty, {})
            .get(harness, _DEFAULT_DIFFICULTY_LEVELS[difficulty].get(harness))
        )
    effort = choice.get("effort", profile.get("effort"))
    return "max" if effort == "max-supported" and profile["harness"] == "claude" else effort


def _classification_profiles(value, context):
    if not isinstance(value, dict):
        raise RoutingError(f"{context} must be a profile or an object containing team")
    if "approval" in value and value["approval"] not in _CONFIRMATIONS:
        raise RoutingError(
            f"{context}.approval must be user or captain (explicit worker confirmation)"
        )
    if "team" in value:
        if set(value) - {"team", "approval", "description"}:
            raise RoutingError(f"{context} supports team, approval and description")
        if "description" in value:
            _text(value["description"], f"{context}.description")
        profiles = value["team"]
        if not isinstance(profiles, list) or not 2 <= len(profiles) <= 8:
            raise RoutingError(f"{context}.team must contain 2 to 8 distinct profiles")
        context += ".team"
    else:
        profiles = [{key: item for key, item in value.items() if key != "approval"}]
    seen = set()
    for index, profile in enumerate(profiles):
        location = f"{context}[{index}]"
        if isinstance(profile, dict) and "team" in profile:
            raise RoutingError(f"{location}.team cannot be nested")
        _profiles(profile, location)
        if not isinstance(profile, dict):
            raise RoutingError(f"{location} must be a profile object")
        _identifier(profile.get("model"), f"{location}.model")
        if profile["harness"] == "pi":
            _provider(profile.get("provider"), f"{location}.provider")
        axes = (
            profile["harness"],
            profile.get("provider", "anthropic" if profile["harness"] == "claude" else None),
            normalize_model(profile["model"]),
        )
        if axes in seen:
            raise RoutingError(f"{context} contains duplicate team peers (harness/provider/model)")
        seen.add(axes)
    return profiles


def _text(value, context):
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        raise RoutingError(f"{context} must be a nonempty string without control characters")


def _identifier(value, context):
    _text(value, context)
    if value != value.strip() or value.startswith("-") or any(char.isspace() for char in value):
        raise RoutingError(f"{context} must be one explicit identifier, not flags or whitespace")


def _provider(value, context):
    if not isinstance(value, str) or not _PROVIDER.fullmatch(value):
        raise RoutingError(
            f"{context} must be a lowercase provider ID, such as a catalog provider key"
        )


def _number(value, context, maximum):
    if type(value) not in (int, float) or not 0 <= value <= maximum or not math.isfinite(value):
        raise RoutingError(f"{context} must be a finite number between 0 and {maximum}")


def _floor(value, context, *, provider_required=False):
    if not isinstance(value, dict):
        raise RoutingError(f"{context} must be an object")
    _text(value.get("scope"), f"{context}.scope")
    _number(value.get("min_percent"), f"{context}.min_percent", 100)
    if provider_required or "provider" in value:
        _provider(value.get("provider"), f"{context}.provider")


def _profiles(value, context):
    profiles = value if isinstance(value, list) else [value]
    if not profiles:
        raise RoutingError(f"{context} must contain at least one profile")
    seen = set()
    for index, profile in enumerate(profiles):
        location = f"{context}[{index}]"
        if not isinstance(profile, dict):
            raise RoutingError(f"{location} must be a profile object")
        if "team" in profile:
            raise RoutingError(f"{location}.team is only supported in classifications")
        unknown = sorted(set(profile) - set(_PROFILE_KEYS))
        if unknown:
            raise RoutingError(
                f"{location} has unsupported keys {unknown}; a profile supports only "
                f"{', '.join(_PROFILE_KEYS)}. Put approval on the rule or classification, "
                "and correct misspelled keys so no requirement is silently ignored"
            )
        _identifier(profile.get("harness"), f"{location}.harness")
        if "model" in profile:
            _identifier(profile["model"], f"{location}.model")
        if "effort" in profile:
            _identifier(profile["effort"], f"{location}.effort")
            if profile["effort"] not in _POLICY_EFFORTS:
                raise RoutingError(
                    f"{location}.effort is unsupported; configure an explicit supported effort"
                )
        if "provider" in profile:
            _provider(profile["provider"], f"{location}.provider")
        if "floor" in profile:
            _floor(profile["floor"], f"{location}.floor")
        axes = tuple(
            normalize_model(profile.get(key)) if key == "model" else profile.get(key)
            for key in ("harness", "provider", "model", "effort")
        )
        if axes in seen:
            raise RoutingError(
                f"{context} contains duplicate profile axes; remove the duplicate candidate"
            )
        seen.add(axes)
    return profiles


def _json_values(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            _json_values(child)
    elif isinstance(value, list):
        for child in value:
            _json_values(child)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise TypeError("Not a JSON value")


def validate_policy(value) -> dict:
    """Return an independent JSON policy, retaining FirstMate extension fields.

    Recognizing a harness in policy does not enable its executor. Unsupported
    executor/effort combinations fail when selected, not by silently adapting.
    """
    if not isinstance(value, dict):
        raise RoutingError("Policy must be a JSON object containing rules and/or default")
    try:
        # Also reject non-JSON extensions and non-finite numbers in unknown fields.
        _json_values(value)
        json.dumps(value, allow_nan=False)
        policy = deepcopy(value)
    except (TypeError, ValueError, RecursionError) as error:
        raise RoutingError("Policy must contain only finite JSON values") from error
    if "difficulty_levels" in policy:
        levels = policy["difficulty_levels"]
        if not isinstance(levels, dict):
            raise RoutingError(
                "difficulty_levels must map difficulty names to harness effort objects"
            )
        normalized_levels = {}
        for name, mapping in levels.items():
            context = f"difficulty_levels.{name}"
            normalized = _difficulty(name, context)
            if normalized in normalized_levels:
                raise RoutingError(f"{context} duplicates a normalized difficulty")
            if not isinstance(mapping, dict) or not mapping:
                raise RoutingError(f"{context} must be a nonempty harness effort object")
            for harness, effort in mapping.items():
                if harness not in _EXECUTOR_EFFORTS:
                    raise RoutingError(f"{context}.{harness} is an unsupported harness")
                _identifier(effort, f"{context}.{harness}")
                if effort not in _EXECUTOR_EFFORTS[harness]:
                    raise RoutingError(f"{context}.{harness} has unsupported effort {effort!r}")
            normalized_levels[normalized] = mapping
        policy["difficulty_levels"] = normalized_levels
    if "classifications" in policy:
        classifications = policy["classifications"]
        if not isinstance(classifications, dict):
            raise RoutingError("classifications must map classification names to profiles or teams")
        for name, profiles in classifications.items():
            _text(name, "classifications name")
            _classification_profiles(profiles, f"classifications.{name}")
    rules = policy.get("rules", [])
    if not isinstance(rules, list):
        raise RoutingError("Policy rules must be an array")
    for index, rule in enumerate(rules):
        context = f"rules[{index}]"
        if not isinstance(rule, dict):
            raise RoutingError(f"{context} must be an object")
        _text(rule.get("when"), f"{context}.when")
        _profiles(rule.get("use"), f"{context}.use")
        if "why" in rule:
            _text(rule["why"], f"{context}.why")
        if "approval" in rule and rule["approval"] != "captain":
            raise RoutingError(f"{context}.approval must be captain")
        if "min_confidence" in rule:
            _number(rule["min_confidence"], f"{context}.min_confidence", 1)
        if "floor" in rule:
            _floor(rule["floor"], f"{context}.floor", provider_required=True)
        if "select" in rule and rule["select"] != "quota-balanced":
            raise RoutingError(f"{context}.select must be quota-balanced")
    if "default" in policy:
        _profiles(policy["default"], "default")
    if not any(key in policy for key in ("rules", "default", "classifications")):
        raise RoutingError("Policy must contain classifications, rules, and/or default")
    return policy


def policy_digest(policy) -> str:
    """Hash canonical validated JSON; formatting and object-key order do not matter."""
    canonical = json.dumps(
        validate_policy(policy), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RoutingError("Policy contains duplicate JSON keys; remove ambiguous declarations")
        result[key] = value
    return result


def _read_policy_file(path):
    descriptors = []
    try:
        absolute = path.absolute()
        descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(descriptor)
        for component in absolute.parts[1:-1]:
            descriptor = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            descriptors.append(descriptor)
        descriptor = os.open(
            absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RoutingError(f"Policy at {path} must be a regular file")
        if metadata.st_size > _MAX_POLICY_BYTES:
            raise RoutingError(f"Policy at {path} exceeds the 1 MiB limit")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            return stream.read(_MAX_POLICY_BYTES + 1)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def load_policy(home, project_root) -> dict:
    """Use the home policy only if the project file is genuinely absent."""
    project_path = Path(project_root) / ".orchestrator" / "crew-dispatch.json"
    home_path = Path(home) / "config" / "crew-dispatch.json"
    for path in (project_path, home_path):
        try:
            content = _read_policy_file(path)
        except FileNotFoundError as error:
            if path.is_symlink():
                raise RoutingError(f"Policy at {path} is a broken symlink; repair it") from error
            continue
        except OSError as error:
            raise RoutingError(
                f"Cannot read policy at {path}; remove symlinks and repair the file or its permissions"
            ) from error
        if len(content) > _MAX_POLICY_BYTES:
            raise RoutingError(f"Policy at {path} exceeds the 1 MiB limit; reduce its size")
        try:
            policy = validate_policy(
                json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
            )
        except (ValueError, UnicodeError, RecursionError) as error:
            raise RoutingError(
                f"Invalid policy at {path}; repair it before dispatch: {error}"
            ) from error
        return {"policy": policy, "digest": policy_digest(policy), "source": str(path)}
    raise RoutingError(
        f"No worker policy configured; create {project_path} or {home_path} with your chosen profiles"
    )


def _execution_axes(profile):
    harness = profile["harness"]
    if harness not in _EXECUTOR_EFFORTS:
        raise RoutingError(
            f"Executor {harness!r} is not enabled. Configure claude for Anthropic or pi with an explicit "
            "provider for other models; direct Codex execution is unsupported"
        )
    if profile["effort"] not in _EXECUTOR_EFFORTS[harness]:
        raise RoutingError(
            f"Effort {profile['effort']!r} is unsupported by {harness}; choose a supported effort "
            "explicitly. Effort will not be downgraded"
        )
    claude_model = "claude" in profile["model"].lower() or model_family(profile["model"])
    provider = profile.get("provider")
    if harness == "pi":
        if provider is None:
            raise RoutingError(
                "Pi requires an explicit provider in the policy or choice; use its catalog provider ID"
            )
        if provider == "anthropic" or claude_model or "anthropic" in profile["model"].lower():
            raise RoutingError(
                "Anthropic/Claude models require the claude executor; update the profile explicitly"
            )
    elif not claude_model or provider not in (None, "anthropic"):
        raise RoutingError(
            "The claude executor requires a Claude model and, if specified, provider anthropic"
        )


def capture_quota_evidence() -> dict:
    """Disclose unavailable evidence without probing credentials or emitting accounts.

    No quota-axi schema adapter is currently trusted by this implementation.
    Merely finding its executable does not establish provider/account matching,
    comparable spend priorities, or completion-feasible runway. Do not execute
    an unknown CLI or pass its raw output (which may contain private accounts).
    """
    return {
        "status": "unavailable",
        "source": "program",
        "reason": "No supported sanitized quota evidence adapter is configured",
        "uncertainty": [
            "Quota, account matching, spend priority, and completion runway are unverified"
        ],
    }


def resolve_selection(
    policy, choice, *, evidence=None, operator_override=False, fallback_difficulty=None
) -> dict:
    """Validate a caller's best-fit decision, without choosing a rule for them.

    `evidence` is reserved for program-captured quota adapters. Caller-supplied
    metrics cannot currently unblock quota gates. Operator overrides bypass
    policy matching, but never executor validation or operator approval.
    A choice's difficulty or effort overrides configured effort; the program's
    `fallback_difficulty` only fills profiles that configure no effort.
    """
    if not isinstance(choice, dict):
        raise RoutingError("Selection must be an object")
    if type(operator_override) is not bool:
        raise RoutingError(
            "operator_override must be a boolean supplied by the operator-only caller"
        )
    _text(choice.get("rationale"), "choice.rationale")
    for axis in ("model", "effort"):
        if axis in choice:
            _identifier(choice[axis], f"choice.{axis}")
    if "difficulty" in choice:
        _difficulty(choice["difficulty"], "choice.difficulty")
    if "difficulty" in choice and "effort" in choice:
        raise RoutingError("choice.effort and choice.difficulty are alternatives; supply only one")
    if "provider" in choice:
        _provider(choice["provider"], "choice.provider")
    if "confidence" in choice:
        _number(choice["confidence"], "choice.confidence", 1)
    rule = {}
    if operator_override:
        _identifier(choice.get("harness"), "choice.harness")
        _identifier(choice.get("model"), "choice.model")
        profile = {key: choice[key] for key in ("harness", "model")}
        profile["effort"] = _effort(profile, choice, {})
        rule_index, candidate_index = "override", None
    else:
        policy = validate_policy(policy)
        if "classification" in choice:
            return _resolve_classification(policy, choice, fallback_difficulty)
        rule_index = choice.get("rule")
        if rule_index == "default":
            if "default" not in policy:
                raise RoutingError(
                    "Policy has no default; choose an explicit best-fit rule or request an operator override"
                )
            profiles = policy["default"]
        elif type(rule_index) is int and 0 <= rule_index < len(policy.get("rules", [])):
            rule = policy["rules"][rule_index]
            profiles = rule["use"]
        else:
            raise RoutingError(
                "choice.rule must be a zero-based rule index or default; choose the best-fitting rule"
            )
        is_array = isinstance(profiles, list)
        candidates = profiles if is_array else [profiles]
        candidate_index = choice.get("candidate", 0 if len(candidates) == 1 else None)
        if type(candidate_index) is not int or not 0 <= candidate_index < len(candidates):
            raise RoutingError(
                "choice.candidate must explicitly identify a zero-based candidate index"
            )
        configured = candidates[candidate_index]
        profile = {"harness": configured["harness"]}
        for axis in ("harness", "model", "provider"):
            configured_value = configured.get(axis)
            chosen_value = choice.get(axis)
            if axis == "model":
                configured_value = normalize_model(configured_value)
                chosen_value = normalize_model(chosen_value)
            if axis in configured and axis in choice and configured_value != chosen_value:
                raise RoutingError(
                    f"choice.{axis} conflicts with the configured profile; omit that axis to use the router, "
                    "or change this project policy through setup_project"
                )
        profile["model"] = configured.get("model", choice.get("model"))
        profile["effort"] = _effort(configured, choice, policy, fallback_difficulty)
        if "provider" in configured:
            profile["provider"] = configured["provider"]
        if "min_confidence" in rule and (
            "confidence" not in choice or choice["confidence"] < rule["min_confidence"]
        ):
            raise RoutingError(
                "Rule confidence threshold is not satisfied; reconsider the selection or escalate"
            )
        if (
            is_array
            or "floor" in configured
            or "floor" in rule
            or rule.get("select") == "quota-balanced"
        ):
            raise RoutingError(
                "Quota-dependent selection requires trusted program-captured evidence for every candidate, "
                "applicable floor, comparable spend priority, and completion runway. Evidence is unavailable; "
                "request an explicit operator override or configure a non-quota-dependent profile"
            )
    if "provider" in choice:
        profile["provider"] = choice["provider"]
    for axis in ("model", "effort"):
        _identifier(profile.get(axis), f"choice.{axis} (or configured profile.{axis})")
    profile["model"] = normalize_model(profile["model"])
    _execution_axes(profile)
    return {
        **profile,
        **(
            {"difficulty": _difficulty(choice["difficulty"], "choice.difficulty")}
            if "difficulty" in choice
            else {}
        ),
        "rule": rule_index,
        "candidate": candidate_index,
        "rationale": choice["rationale"],
        "requires_approval": operator_override or rule.get("approval") == "captain",
        "evidence": capture_quota_evidence(),
        "uncertainty": ["Quota availability and model catalog support have not been verified"],
    }


def _resolve_classification(policy, choice, fallback_difficulty):
    classification = choice["classification"]
    _text(classification, "choice.classification")
    if classification not in policy.get("classifications", {}):
        raise RoutingError(
            f"choice.classification {classification!r} is not configured; "
            f"choose one of {list(policy.get('classifications', {}))}, "
            "or add it to this project policy through setup_project"
        )
    for field in ("model", "harness", "provider", "rule", "candidate", "team"):
        if field in choice:
            raise RoutingError(
                f"choice.{field} cannot accompany classification; the policy supplies worker profiles"
            )
    configured = policy["classifications"][classification]
    profiles = _classification_profiles(configured, f"classifications.{classification}")
    resolved = []
    for profile in profiles:
        selection = resolve_selection(
            {"default": profile, "difficulty_levels": policy.get("difficulty_levels", {})},
            {key: value for key, value in choice.items() if key != "classification"}
            | {"rule": "default"},
            fallback_difficulty=fallback_difficulty,
        )
        selection.update(classification=classification, rule=classification, candidate=None)
        resolved.append(selection)
    requires_approval = configured.get("approval") in _CONFIRMATIONS
    if "team" not in configured:
        return resolved[0] | {"requires_approval": requires_approval}
    return {
        "team": resolved,
        "classification": classification,
        **(
            {"difficulty": _difficulty(choice["difficulty"], "choice.difficulty")}
            if "difficulty" in choice
            else {}
        ),
        **({"effort": choice["effort"]} if "effort" in choice else {}),
        "rule": classification,
        "candidate": None,
        "rationale": choice["rationale"],
        "requires_approval": requires_approval,
        "team_description": configured.get("description", ""),
        "evidence": capture_quota_evidence(),
        "uncertainty": ["Quota availability and model catalog support have not been verified"],
    }


def policy_readiness(policy) -> dict:
    """Validate configured selections without dispatch, inference, or quota guesses."""
    policy = validate_policy(policy)
    profiles = [(index, rule["use"]) for index, rule in enumerate(policy.get("rules", []))]
    if "default" in policy:
        profiles.append(("default", policy["default"]))
    executable_profiles = 0
    blockers = []
    for rule, choices in profiles:
        for candidate, profile in enumerate(choices if isinstance(choices, list) else [choices]):
            choice = {
                "rule": rule,
                "candidate": candidate,
                **(
                    {"effort": profile["effort"]} if "effort" in profile else {"difficulty": "hard"}
                ),
                "rationale": "Policy readiness validation",
                "confidence": 1,
            }
            if "provider" in profile:
                choice["provider"] = profile["provider"]
            try:
                resolve_selection(policy, choice)
                executable_profiles += 1
            except RoutingError as error:
                blockers.append({"rule": rule, "candidate": candidate, "reason": str(error)})
    for classification, configured in policy.get("classifications", {}).items():
        classification_profiles = _classification_profiles(
            configured, f"classifications.{classification}"
        )
        # Readiness asks whether each configured model can execute at a supported
        # effort, not whether the coordinator has already classified a task's difficulty.
        for candidate, profile in enumerate(classification_profiles):
            try:
                resolve_selection(
                    {"default": profile, "difficulty_levels": policy.get("difficulty_levels", {})},
                    {
                        "rule": "default",
                        **(
                            {"effort": profile["effort"]}
                            if "effort" in profile
                            else {"difficulty": "hard"}
                        ),
                        "rationale": "Policy readiness validation",
                    },
                )
            except RoutingError as error:
                blockers.append(
                    {"rule": classification, "candidate": candidate, "reason": str(error)}
                )
                break
        else:
            executable_profiles += len(classification_profiles)
    return {
        "valid": True,
        "routable": executable_profiles > 0,
        "executable_profiles": executable_profiles,
        "blockers": blockers,
        "message": "Model catalog support and access have not been verified. "
        "Schema validation does not authorize execution. "
        "Workers still need an explicit selection and any required approval.",
    }
