# Worker routing and execution contract

This document records implementation boundaries for worker routing and execution.
See [project routing](project-routing.md) for the current conversational API and policy schema, and [workers](workers.md) for operational behavior.
The original single-worker implementation remains the foundation, but its monitor-only planned selection and operator-only approval restrictions no longer describe current behavior.
Worker models come from project policy, not automatically populated defaults.
The four core roles retain configured models and efforts; shared supervisor safeguards do not add per-role dollar budgets, timeouts, CPU, or memory fields.

## Scope and authority

Enable routing and workers independently of policy availability; missing policy blocks selection rather than guessing a profile.
Load project `.orchestrator/crew-dispatch.json` before the home fallback, and never fall back from invalid project policy.
Support named classifications containing one profile or a read-only comparison team, plus legacy FirstMate-compatible rules/default.
The coordinator interprets classification and difficulty; legacy natural-language rules are best-fit guidelines, not keyword matches or a separate routing model.
The bound coordinator may select planned and on-demand workers while preserving project, plan, node, and original user-event identities.
The monitor may select only supplied pending legacy plan requests through its accepted result.
Classification suggestions become `worker.routing_recommended` notifications rather than executable selections or failed monitor reviews; final effort remains the coordinator's decision.
Explicit policy-match overrides remain a separate CLI-only capability, never a model-supplied authority label.
Plain version: preserve who requested the work and which plan it belongs to, while letting the coordinator choose its configured workers.

Pin concrete harness/model/effort and policy provenance before dispatch, with no silent provider fallback.
Use Claude Code for Anthropic specialists and Pi with an explicit provider for other models.
Reject unsupported effort rather than lowering it silently.
Legacy quota-dependent arrays require trusted evidence and are not comparison teams.
Classification difficulty mappings and supported profile schemas are documented in [project routing](project-routing.md#classifications-and-difficulty).

Read workers are read-only; write workers edit only isolated Git worktrees, never the source checkout.
Both harnesses expose controlled file tools, not unrestricted shell or unreviewed extensions.
These controls are not an operating-system sandbox against hostile same-user processes.
There is no automatic merge or push.
Permissions default to conversational coordinator approvals enabled, general write approval not required, and monitor holds enforced; these are project-local configurable preferences.
A routing rule's explicit approval requirement and policy-match overrides still require approval.
Plan approval still requires independent review and fresh monitor evidence.
Successful execution produces a candidate, not accepted completion; checked acceptance releases dependencies.
Approval nodes cannot launch workers, and failed prerequisites block or cancel dependents according to configuration.
Unknown outcomes are never automatically replayed.

## Policy and project configuration modules

`orchestrator.routing` validates classifications, difficulty mappings, legacy rules/default, and concrete selections without interpreting task prose.
`validate_policy` returns a defensive validated copy, and `load_policy` returns policy, digest, and source without a guessed default.
`resolve_selection` checks the caller's classification or legacy choice and returns a concrete profile or team with provenance and disclosed uncertainty.
Difficulty or exact effort may override optional profile effort; model, harness, and provider still come from the configured classification.
`capture_quota_evidence` reports unavailable evidence until a trusted adapter exists; model-reported quota numbers are not authoritative.

`orchestrator.onboarding` supports repeatable project-local policy saves, incomplete drafts, and repair without a permanent setup seal.
`orchestrator.project_settings` validates partial settings and writes only the bound project's private JSON override, never global/local/default configuration or another project.
Arbitrary executable commands are not conversational settings because their effects need not stay within a project.
Role/model settings are captured for tasks, while permissions, concurrency, and worker/routing disable settings remain live at enforcement points.
Plain version: future tasks can use new model choices, while current permission and scheduling rules still control what may proceed.

## Execution and workspace boundaries

`orchestrator.worker_execution` builds restricted harness commands and prepares isolated workspaces with repository, base, branch, and dependency provenance.
Snapshot preparation rejects dirty source state except sole regular routing-policy additions, modifications, or deletions, including tracked and staged changes.
No stash, source commit, ignore rule, or Git exclude change is needed for that policy exception.
Accepted dependency commits are applied only to isolated workspaces; conflicts fail without changing the source checkout.
Bounded diffs and signed local candidate commits capture results; centrally configured identity and signing are never overridden or bypassed.
Controlled Git operations disable hooks and external diff/textconv and reject unsafe metadata paths, filters, and merge drivers.
Ordinary non-Git read-only work remains supported; comparison teams require a frozen Git baseline.
Plain version: keep source changes separate and preserve evidence of exactly which files each worker saw and changed.

## Durable worker and team services

The original single-worker foundation added schema migration version 2, worker policy snapshots and requests, task links, and plan origin links.
That is historical migration context, not an instruction to reset the current schema version or a complete schema for team support.
Current migrations and tables are defined in `orchestrator/workers.py` and `orchestrator/teams.py`; preserve existing projects, jobs, inboxes, and notes.
Requests retain immutable ownership and origin relationships, policy provenance, approval, task links, workspaces, and results.
Use transactions and generation/state checks for authority transitions.

`WorkerService.select` supports bound planned and on-demand requests and automatically refreshes unattempted selections to current policy, clearing prior approval.
Explicit refresh also invalidates the old choice and approval where the request remains eligible.
Queued and running jobs retain captured selections; later policy edits do not revoke or replay them.
Attempted requests cannot be blindly refreshed or reselected.
`approve`, `accept`, and `approve_node` are available through authorized project-scoped APIs as well as optional CLI commands; `override` remains CLI-only.
Dispatch and start checks enforce current ownership, dependencies, applicable holds and permissions, pauses, concurrency, and enable/disable controls.
Runtime config and concrete profile snapshots do not justify ignoring those live checks.
Monitor result processing validates supplied request context and atomically accepts findings, holds, recommendations or legacy selections, cursor, and processed acknowledgment.
Language meaning still requires judgment; software cannot prove arbitrary prose is unrelated work.

Teams use ordinary durable child requests for two rounds over the same frozen Git commit and accepted dependencies.
Round two receives complete untrusted round-one reports rather than instructions or a real-time peer conversation.
Normal concurrency and resource limits apply, so four peers produce eight jobs, not eight simultaneous processes.
Only parent acceptance can release the associated graph node; children cannot independently authorize completion.
Cancellation applies to the parent and children, with terminal confirmation and no blind replay of uncertain outcomes.
See [project routing](project-routing.md#read-only-comparison-teams) for team bounds and evidence behavior.

## Runtime and transport integration

Seed approved plan requests and dispatch eligible selections without bypassing graph readiness.
Worker tasks capture request identity, concrete profile, mode, project root, and accepted dependency inputs.
Core role configuration remains orchestrator/planner/critic/monitor; workers use `roles/worker.md` and shared supervisor limits.
Reports have the strict shape `{summary: string, changes: [string], checks: [string], remaining_issues: [string]}` and are stored alongside program-captured provenance.
Report claims do not independently prove tests passed.

Expose project-scoped setup/settings, policy inspection, request/get/list, planned and on-demand selection, refresh, approvals, acceptance, and cancellation through the common API/MCP/Pi bridge.
Keep foreground tool guards: no shell, direct source writes, untracked implementation agents, or arbitrary executable replacement.
Native observers display already dispatched workers and team children without owning execution or opening Herder windows.
Project ownership, dependency correctness, source isolation, credential limits, and no unknown-outcome retry remain intrinsic boundaries even when optional gates are disabled.
Plain version: the coordinator can make authorized decisions in chat, but every worker stays tracked and every accepted result still needs evidence.
