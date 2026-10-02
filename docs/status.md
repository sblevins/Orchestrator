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

`execution.mode` distinguishes guarded coordination from trusted native foreground tools and makes owned worker `run_command` available; `commands.enabled` can add it in restricted mode.
Trusted commands are unsandboxed, restricted commands are OS-sandboxed unless `commands.sandbox = false`, and isolated worker worktrees are not operating-system security boundaries.
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

## Plan presentation

This checkout includes shared plan presentation for Claude MCP and Pi through `export_plan`, with a standardized offline HTML/SVG page, Mermaid source, and JSON snapshot.
The coordinator links the returned `html_uri` rather than generating page code; the planner still emits only a structured graph.
The bundle uses private project storage, saved worker selections, and dependency-depth waves, not inferred models or promised simultaneous launches.
It remains static, with no editor, polling, approval, dispatch, or bounded review-loop node.
See [workflows](workflows.md#present-a-saved-plan) for the API, CLI, privacy boundary, and refresh instructions.
The renderer uses the existing luxury/silk palette, works offline, and does not need a model to write page markup.
Plain version: one tool call makes a private plan page; the page does not update itself or start work.

Saved plans can optionally include active-work estimate ranges and explicit unrolled cycle round labels, such as review/revise rounds.
The standard view displays task ranges, ideal parallel wave ranges (unknown if any task estimate is missing), and linked round groups.
These are presentation metadata, not scheduling deadlines or executable early-stop loops.

## Validation

The full offline suite passes with Python resource warnings treated as errors and an inherited validation-worker marker.
This includes the optional real Mermaid parser and Chromium checks using temporary development tools, with no skipped tests in that run.
Those temporary tools are test-only; exporting and viewing the HTML page needs no installed browser automation or Mermaid runtime.
Artifact tests cover private publication, project isolation, real CLI/MCP calls, saved profiles and team rounds, exclusion of worker prompts, unavailable readiness under invalid settings, injection attempts, 256-node graphs, offline rendering, theme and zoom controls, and no-JavaScript viewing.
Tests exercise real signed Git workspaces, dirty and unborn source snapshots, submodules, command execution and parent-death cleanup, peer messaging, quiet monitor delivery, standing authorization, critic-only retry, and the installed Pi SDK against a loopback fake provider.
Image tests use a mocked transport and real binary artifact publication, with no paid image requests.
Independent review identified and prompted fixes for stalled completion callbacks, observer turn rejection, and synthetic team issues that prevented unattended acceptance.
A separate external-validation regression reproduces and fixes hooks blocking reviewer shell tools and structured output; it also verifies the long Stop watcher remains inert.
No paid application-provider requests were made.
Offline fake-provider tests cannot prove paid model access, answer quality, image billing, live Claude rendering, voice behavior, or idle wakeup.
Plain version: local tests check the saved pages and program behavior; paid model access and live coordinator conversations are separate checks.

## Integration and remaining limits

The command, image, model-selector, peer-message, review-retry, monitoring, and unattended helpers are integrated and covered by offline tests.
Source submodule pointer changes and uncommitted submodule contents still require separate handling rather than being silently omitted from a parent snapshot.
Quota-dependent candidate arrays and floors fail closed without a supported sanitized quota evidence adapter.
Pi's installed observer plugin still has FleetView and unfinished-text limitations.
The supervisor is single-host and has no installed boot service; an OS restart requires starting it again.
Live provider entitlement, exact effort support, frontend behavior, and workload quality, latency, and cost remain unverified.

## Deployment activation

The prior routing and trusted-execution update was merged in PR #1 and its root supervisor was updated to commit `2e6b67e`.
Plan visualization is a separate change and has not been activated in that running checkout.
After validation and merge, update the checkout and reload the native frontend's tool definitions to expose `export_plan`.
No live process or user project state was changed during visualization development.
Plain version: the earlier update is installed, but this new page tool still needs delivery and a frontend reload.

## Next step

Complete independent review and delivery checks for plan presentation before activation.
The earlier hook-related validation failure was fixed before PR #1 merged; it is not an outstanding blocker.
Obtain separate authorization before paid provider acceptance checks.
