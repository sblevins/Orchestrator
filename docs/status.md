# Project status

## Goal

A project-bound conversational coordinator for Claude Code and Pi, with independent planning and criticism, quiet background monitoring, and durable workers chosen through project routing.
The deterministic supervisor owns task state, dependencies, authorization checks, and result acceptance.
Plain version: models do the work while software remembers what is allowed and what has finished.

## Implemented

This section describes this feature checkout, not the older running deployment.
The Python/SQLite supervisor persists projects, sessions, notes, graphs, events, inboxes, worker attempts, cancellations, and restart reconciliation.
Detached runners use resource reservations, deadlines, strict result parsing, and process-identity checks.
Unknown outcomes are not blindly replayed.

Project-local configuration is repeatable through `configure_project` and `setup_project`, including repairs and changes after dispatch.
It never changes another project's settings or shared defaults.
Routing supports classifications, caller-selected difficulty or effort, and legacy rules/default profiles without automatic worker model defaults.
The bound coordinator selects both planned and on-demand workers.
Monitor classification suggestions remain recommendations unless unattended authorization permits selection with configured profile effort or `execution.worker_difficulty`.

`execution.mode` distinguishes guarded coordination from trusted native foreground tools and owned worker `run_command` execution.
Trusted commands are unsandboxed and can run checks; isolated worker worktrees are not operating-system security boundaries.
`execution.base_ref` selects the Git branch used to prepare worker checkouts.
Trusted `HEAD` work snapshots unfinished source files without committing or changing the source checkout.
Internal instruction links and registered submodule initialization work in trusted checkouts.
Project permission gates remain configurable, and source worktrees are not automatically merged or pushed.
`execution.unattended` supplies standing project authorization to approve reviewed plans and accept candidate results with an explicit empty `remaining_issues` list, subject to existing live gates.
It does not supply explicit worker approval required by policy, including security-audit teams.
Only team parents can be accepted.
Plain version: the project can authorize routine progress in advance, but required worker approval and unresolved problems still stop it.

Planning instructions require clarifying material unknowns before a paid planner call, followed by independent final review.
`retry_review` retries an eligible failed critic against its saved draft without paying for a new planner run; unknown outcomes are not replayed.
The monitor waits for foreground completion and `monitoring.quiet_seconds` (20 by default).
Failed Pi completion notifications retry automatically, and a verified dead frontend does not keep monitoring blocked.
Only blocking monitor findings interject; informational and warning findings remain silently available for inspection and acknowledgment.

Read-only comparison teams use 2 to 8 distinct configured models over the same frozen Git baseline and accepted dependencies in two rounds.
Peers also have durable live `send_team_message` and `read_team_messages` tools; messages and reports are untrusted evidence, not instructions or permission.
Claude and Pi native observers display existing jobs without owning execution or acceptance.

Model selectors include Anthropic families and Pi's `Astra` and `Sol` families, resolved within the installed provider catalog with exact IDs taking precedence.
Pi worker `max-supported` requests the highest effort supported by the selected model rather than an unsupported fixed effort.
Explicit image generation uses the separately billed OpenAI Images API and `OPENAI_API_KEY`, not subscription OAuth.
Project `images.enabled` defaults to false; outputs are PNG or JPEG.
Plain version: model names follow the available catalog, and making an image is a separate paid action that must be enabled.

## Validation

The integrated checkout passes 505 offline tests with Python resource warnings treated as errors.
Ruff lint and formatting, Node syntax checks, and `git diff --check` pass.
Tests exercise real signed Git workspaces, dirty and unborn source snapshots, submodules, command execution and parent-death cleanup, peer messaging, quiet monitor delivery, standing authorization, critic-only retry, and the installed Pi SDK against a loopback fake provider.
Image tests use a mocked transport and real binary artifact publication, with no paid image requests.
Independent review identified and prompted fixes for stalled completion callbacks, observer turn rejection, and synthetic team issues that prevented unattended acceptance.
A separate external-validation regression reproduces and fixes hooks blocking reviewer shell tools and structured output; it also verifies the long Stop watcher remains inert.
The suite also passes with an inherited validation-worker marker.
No paid application-provider requests were made.
Offline fake-provider tests cannot prove paid model access, answer quality, image billing, live Claude rendering, voice behavior, or idle wakeup.
Plain version: the automated local checks pass; real paid-model and interactive frontend checks remain separate.

## Integration and remaining limits

The command, image, model-selector, peer-message, review-retry, monitoring, and unattended helpers are integrated and covered by offline tests.
Source submodule pointer changes and uncommitted submodule contents still require separate handling rather than being silently omitted from a parent snapshot.
Quota-dependent candidate arrays and floors fail closed without a supported sanitized quota evidence adapter.
Pi's installed observer plugin still has FleetView and unfinished-text limitations.
The supervisor is single-host and has no installed boot service; an OS restart requires starting it again.
Live provider entitlement, exact effort support, frontend behavior, and workload quality, latency, and cost remain unverified.

## Deployment activation

The existing running deployment has not been activated on this checkout's new behavior.
After validation, upgrade and restart the supervisor and native frontend together for the schema and tool changes.
No live process was restarted as part of this documentation update.
Plain version: existing sessions still need an upgrade before they can use the new features.

## Next step

The initial delivery review was blocked by the hook bug and timed out; rerun the independent delivery gate with that fix before activation.
Obtain separate authorization before paid provider acceptance checks.
