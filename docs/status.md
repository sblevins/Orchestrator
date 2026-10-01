# Project status

## Goal

A fast, project-bound conversational coordinator for Claude Code and Pi, with independent planning and criticism, a slow ongoing monitor, and temporary workers chosen under First Mate's routing guidelines.
The deterministic supervisor owns task state and dependency enforcement, with explicit operator authorization and acceptance before work advances.
Plain version: models propose and perform work, while software remembers it and waits for your approval when required.

## Implemented

The local Python/SQLite foundation supports project/session ownership, notes, durable events and inboxes, graph validation/readiness, planner-to-critic handoff, bounded revisions, monitor review cursors, blocking holds, and explicit operator approval.
Detached resource-reserved runners have deadlines, cancellation, process-identity checks, strict result parsing, and restart reconciliation.
Claude Code hooks/MCP and the owned Pi extension share this state.
Shared supervisor settings retain resource reservations and task deadlines, without per-role dollar caps.
The public static explainer in `docs/explainer/` retains its theme, layout, and diagram without a feedback server or private runtime data.

The worker implementation enables routing and execution without supplying any worker model defaults.
Project `.orchestrator/crew-dispatch.json` policy, explicit profile selection, and authorization gates are required before dispatch.
The slow monitor selects plan work; the foreground selects unrelated work.
Write workers use isolated worktrees and produce signed local candidates, not automatic source-branch merges or pushes.
Operator plan approval authorizes declared write nodes; unrelated writes and policy-required approvals require worker approval.
Explicit result acceptance completes graph nodes and unlocks dependent work.
See [workers](workers.md) for policy setup, authority, and acceptance.
Plain version: a worker can prepare changes separately, but finishing the job does not approve those changes.

Claude Code executes Anthropic models only; other models use the owned Pi SDK bridge pinned to 0.99.2.
The critic is Pi `openai-codex` / `gpt-6-astra` / `high`.
Pi verifies the exact model, effort, and tool allowlist without silent fallback, uses fresh ephemeral sessions without resume, and reuses canonical auth without inherited plugins.
Worker tools permit reading and file edits only, not shell commands or tests.
See [adapters](adapters.md) for execution and authentication boundaries.

## Validation

Last verified 2026-10-01: all 238 tests passed under a `machine-resources` reservation with resource warnings treated as errors.
The suite includes real CLI worker selection/approval/acceptance, monitor-owned planned dispatch, dependency gating, signed isolated write results, and local Pi SDK tests against a loopback-only fake provider.
The subprocess lifecycle tests also passed three consecutive runs after correcting a cleanup lock race.
Ruff lint/format, Node syntax, and `git diff --check` passed.
Independent review findings were reproduced and fixed: stale dependency reads, oversized valid plan seeding, and cancellation during final launch preparation.
Policy special-file handling and failed SQLite connection cleanup also have regression coverage.
These checks do not prove paid model access, answer quality, native microphone behavior, or live Claude idle wakeup.
Plain version: the local checks passed, but real provider access and interactive behavior still need separate verification.

## Explicitly pending

- A supported sanitized quota evidence adapter is unavailable, so quota-dependent candidate arrays, floors, and quota-balanced selection fail closed.
- Operator-chosen worker profiles must be populated; there are no automatic worker model defaults.
- Live exact-model/effort acceptance, native Claude voice/wake checks, and workload-specific quality, latency, and cost evaluation remain unverified.
- Optional OS-managed supervisor restart and any third-party Pi orchestration plugin or shared graph UI remain outside the current implementation.

The supervisor is single-host, not a distributed Temporal deployment, and has no installed boot service.
Unknown work is not blindly replayed.
Claude's reported resumed-session costs must not be summed as verified per-turn spending; Pi dollar cost remains unknown.
File-tool controls are not an operating-system sandbox against hostile same-user processes.
Plain version: missing quota information stops quota-based selection, and limited model tools do not isolate other programs on the machine.

## Next decision

Configure the desired worker profiles, then verify live access and review a candidate before explicitly accepting it.
Quota-dependent policy must wait for supported evidence or an explicit operator override; it must not silently choose another profile.

## In flight

No unfinished implementation worktree or open pull request remains in the current repository evidence.
Live provider and interactive acceptance checks remain pending as listed above.
