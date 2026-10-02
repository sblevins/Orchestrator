# Project status

## Goal

A fast, project-bound conversational coordinator for Claude Code and Pi, with independent planning and criticism, a slow ongoing monitor, and temporary workers chosen under First Mate's routing guidelines.
The deterministic supervisor owns task state and dependency enforcement, with explicit operator authorization and acceptance before work advances.
Plain version: models propose and perform work, while software remembers it and waits for your approval when required.

## Implemented

This section describes integration commit `c45f31f`; the tested replacement behavior awaiting activation is listed under In flight.

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

Initial routing setup is conversational in both frontends through `project_setup` and `setup_project`.
The active coordinator can initialize and validate the bound project's policy using user-chosen preferences, without an operator-shell handoff.
Initial setup closes after configuration or dispatched work, and deleting the policy does not reopen it.
A sole untracked regular worker-policy file no longer blocks otherwise clean write-workspace preparation; source changes and tracked policy edits still do.
Plain version: the coordinator can finish its setup without receiving permission to change code.

Claude Code executes Anthropic models only; other models use the owned Pi SDK bridge pinned to 0.99.2.
The critic is Pi `openai-codex` / `gpt-6-astra` / `high`.
Pi verifies the exact model, effort, and tool allowlist without silent fallback, uses fresh ephemeral sessions without resume, and reuses canonical auth without inherited plugins.
Worker tools permit reading and file edits only, not shell commands or tests.
See [adapters](adapters.md) for execution and authentication boundaries.

Role and worker models accept case-insensitive Opus, Sonnet, Haiku, and Fable family selectors alongside unchanged exact IDs.
Claude uses native aliases; the Pi foreground selects the newest stable matching version in its loaded provider catalog without fuzzy sorting or authentication fallback.
Task records distinguish the requested selector from reported model usage, which can include auxiliary models.
Existing defaults and private version pins are unchanged.
See [model families](configuration.md#model-families-or-exact-versions) for configuration and freshness limits.

Frontend-specific observers add native visibility without changing worker execution ownership.
Claude uses exactly authorized Haiku watchers; Pi uses local no-LLM observers through the installed pi-subagents public RPC.
MCP watches are concurrent and bounded, project/session checked, and safely detached on cancellation or disconnect.
Native stop never cancels the durable worker; acceptance remains an operator action.
See [worker visibility](worker-visibility.md) for reattachment and installed UI limitations.

## Validation

Integration commit `c45f31f` last passed 311 tests on 2026-10-01.
The pending flexible-project-routing branch passed all 347 tests on 2026-10-01 under a `machine-resources` reservation with resource warnings treated as errors.
Ruff lint/format and `git diff --check` also passed for that branch.
The suite includes real CLI worker selection/approval/acceptance, monitor-owned planned dispatch, dependency gating, signed isolated write results, and local Pi SDK tests against a loopback-only fake provider.
Family tests cover a real CLI-to-worker run with a fake harness, installed Pi startup, numeric ordering, exact-pin preservation, provider boundaries, reload behavior, and fenced model-usage records.
Onboarding tests reproduce the original block through real MCP/hook processes, exercise initial setup and permanent closure, and verify signed workspace preparation with untracked setup metadata.
Cooperating setup writes serialize with revision checks; manual file editors must not run concurrently with conversational setup.
The subprocess lifecycle tests also passed three consecutive runs after correcting a cleanup lock race.
Ruff lint/format, Node syntax, and `git diff --check` passed.
Independent review findings were reproduced and fixed: stale dependency reads, oversized valid plan seeding, and cancellation during final launch preparation.
Policy special-file handling and failed SQLite connection cleanup also have regression coverage.
Temporary Git fixtures are isolated from machine-wide configuration; the full suite passed with a CI-like global filter present, while production filter rejection remains tested.
The actual installed Pi/plugin creates native observer records under a network-denying, credential-free test environment with no extra foreground model turns.
Exact Claude watcher invocation, output backpressure shutdown, lost Pi spawn replies, stale-generation responses, and detach-only cancellation have regression coverage.
These checks do not prove paid model access, answer quality, native microphone behavior, live Claude watcher rendering, or live Claude idle wakeup.
Plain version: the local checks passed, but real provider access and interactive behavior still need separate verification.

## Explicitly pending

- A supported sanitized quota evidence adapter is unavailable, so quota-dependent candidate arrays, floors, and quota-balanced selection fail closed.
- Operator-chosen worker profiles must be populated; there are no automatic worker model defaults.
- Live exact-model/effort acceptance, native Claude voice/wake checks, and workload-specific quality, latency, and cost evaluation remain unverified.
- Pi's installed plugin does not reliably initialize automatic FleetView from RPC or show unfinished observer text; upstream UI fixes are needed.
- Live Claude watcher UI and permission-dialog acceptance remain unverified.
- Optional OS-managed supervisor restart and a shared graph UI remain outside the current implementation.

The supervisor is single-host, not a distributed Temporal deployment, and has no installed boot service.
Unknown work is not blindly replayed.
Claude's reported resumed-session costs must not be summed as verified per-turn spending; Pi dollar cost remains unknown.
File-tool controls are not an operating-system sandbox against hostile same-user processes.
Plain version: missing quota information stops quota-based selection, and limited model tools do not isolate other programs on the machine.

## Next decision

Tell the coordinator the desired worker profiles and let it complete initial setup, then verify live access and review a candidate before explicitly accepting it.
Quota-dependent policy must wait for supported evidence or an explicit operator override; it must not silently choose another profile.

## In flight

`feature/flexible-project-routing` is implemented and locally verified in `worktrees/flexible-project-routing`, based on current `origin/main`.
It removes permanent setup locks, adds bound-project settings and conversational approvals, separates classification-based models from coordinator-chosen difficulty, and runs durable two-round read-only comparison teams.
Project configuration writes never modify another project's settings or shared defaults.
Tests cover four different fixture models across eight real offline runner jobs, a pinned code baseline after source HEAD changes, complete peer report exchange, cancellation, restart, and parent-only acceptance.
Independent review found and prompted fixes for monitor classification authority and approval responses after committed state changes.
No paid provider calls were made.
Activation is pending because the existing supervisor (PID `3215998`, reservation `424de9`, directory `~/Agents/Orchestrator`) still runs old code; it and the native frontend must be restarted for the schema/tool upgrade.
The live processes and the root checkout's unrelated startup-warning edit remain untouched.
Plain version: the changes pass local checks, but the current running coordinator has not been upgraded.
