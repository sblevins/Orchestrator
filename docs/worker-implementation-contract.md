# Worker routing and execution contract

This is the implementation contract for the current worker-router change.
The user has authorized implementing workers, but will configure worker profiles inside each project.
No active worker model or effort defaults are to be supplied.
The four existing core roles retain their configured models and efforts.
Shared supervisor safeguards remain; no per-role dollar budgets, timeouts, CPU, or memory fields return.

## Scope and authority

Enable the routing and worker capabilities independently of policy availability.
Without a valid configured policy, work remains blocked rather than guessing a profile.
Load FirstMate-compatible JSON from `<project-root>/.orchestrator/crew-dispatch.json`, falling back to `<home>/config/crew-dispatch.json` only when the project file is absent.
An invalid project policy must not silently fall back.
Rules are best-fit natural-language guidelines interpreted by the existing model roles, not ordered keyword matches and not a new router model.

The slow monitor chooses plan-associated workers, through its accepted result only.
The foreground coordinator chooses unrelated workers through the project-scoped API.
Explicit operator overrides are a separate CLI-only authority, never a model-supplied role label.
Pin concrete harness/model/effort and the policy digest before dispatch; do not silently change providers on failure.
Arrays must honor evidence and quota constraints or escalate rather than inventing a ranking.
Use Claude Code only for Anthropic models and Pi for all other models, for both core roles and workers.
Pi profiles pin a provider, model, and supported thinking level.
Direct Codex execution is not an enabled route; recognizing another FirstMate harness in the policy does not implement its executor.
Reject unsupported effort levels rather than silently lowering them.

Read workers are read-only.
Write workers require an operator-approved write-capable plan node or explicit operator approval of the individual request.
A FirstMate `approval: captain` rule always adds operator approval, including within an approved plan.
Workers may write only in an isolated Git worktree, never the user's source checkout.
Claude write workers get restricted file-edit tools, not unrestricted Bash.
Pi write workers receive only controlled file tools, without Bash or unreviewed extensions.
Pi is not an operating-system sandbox; isolate its configuration and enforce project path limits in owned tools before enabling writes.
No automatic merge or push into the user's working branch.

A successful harness produces a candidate result, not accepted completion.
An operator accepts results with a CLI command after reviewing the saved output/worktree/diff.
Only acceptance completes the graph node and releases its dependents.
Approval nodes require a separate operator action and cannot launch workers.
A failed prerequisite blocks or cancels dependent readiness according to the existing preference.
Unknown outcomes are never automatically replayed.
Active mid-turn steering is outside this first execution increment; cancellation and explicit follow-up requests remain available.

## Policy module: `orchestrator.routing`

- `RoutingError(ValueError)` provides actionable reasons.
- `validate_policy(value) -> dict` returns a defensive validated copy of FirstMate's `rules`/`default` structure.
- `load_policy(home, project_root) -> dict` returns `{policy, digest, source}`; missing or invalid policies raise `RoutingError` without a guessed default.
- `resolve_selection(policy, choice, *, evidence=None, operator_override=False) -> dict` validates the selector's decision and returns a concrete profile plus provenance and `requires_approval`.
- `choice` for normal decisions contains `rule` (zero-based integer or `default`), `candidate` (zero-based, default 0 for a single profile), `model`, `effort`, and nonempty `rationale`; optional `confidence` supports an explicit rule threshold.
- Explicit profile model/effort must match; omitted axes must be resolved explicitly by the selector, not inherited opaquely from a foreground model.
- Maximum effort requires an explicit policy preference or operator override.
- Operator override choices contain concrete `harness`, `model`, `effort`, and `rationale` and bypass policy matching, not execution/approval gates.
- Return profile fields `harness`, `model`, `effort`, optional `provider`, `rule`, `candidate`, `rationale`, `requires_approval`, and any disclosed evidence/uncertainty.
- `capture_quota_evidence() -> dict` supplies bounded program-captured evidence or a disclosed unavailable state; model-provided quota measurements are not authoritative.

## Execution module: `orchestrator.worker_execution`

- `build_worker_command(config, profile, mode, prompt, cwd, output_path, *, project_root=None) -> list[str]` uses stdin prompts and explicit axes; mode is `read` or `write`.
- `prepare_workspace(home, request, project_root, dependency_commits=()) -> dict` creates an isolated Git worktree for write work and returns its path and repository/base/branch provenance.
- A write project must be a Git repository with a clean source checkout; no stashing or overwriting user changes.
- Accepted dependency commit IDs are applied to the isolated workspace; conflicts fail visibly without changing the source checkout.
- `capture_workspace(workspace) -> dict` records a bounded diff artifact, file list and commit provenance after the worker exits.
- Internal result commits remain local, signed by the centrally configured identity; no identity overrides or signing bypass.
- Git commands must disable hooks and external diff/textconv for controlled artifact operations, and protect workspace metadata from path redirection.
- Non-Git read-only work is supported without creating a write workspace.

## Durable service: `orchestrator.workers.WorkerService(store)`

Use additive schema migration version 2, never resetting `user_version` to 1 when reopening.
Add worker policy snapshots and requests, a nullable worker-request link on tasks, and an optional origin-event link on plans.
Preserve existing projects, jobs, inboxes, and notes.
Requests retain immutable project/plan/node/mode/origin relationships, selected policy/provenance, approval, task link, workspace and result state.
Use transactions and generation/state checks for all authority transitions.

Methods used by the parent integration:

- `request(session_id, brief, mode='read', *, origin_event_id, plan_id=None, node_id=None, idempotency_key=None) -> dict`
- `get(request_id) -> dict` and `list(project_id) -> list[dict]`
- `policy(project_id) -> dict` exposes current policy status and shared procedure, not credentials.
- `select(session_id, request_id, choice) -> dict` permits unrelated requests only.
- `override(request_id, choice) -> dict` is operator-only; never expose through MCP/Pi.
- `approve(request_id, reason) -> dict` authorizes a pending individual request through the operator CLI.
- `accept(request_id, reason) -> dict` accepts a completed candidate result and completes its graph node.
- `approve_node(plan_id, node_id, reason) -> dict` handles approval-only nodes with dependency and hold checks.
- `refresh(session_id, request_id) -> dict` explicitly refreshes a not-running request's policy snapshot, invalidating its old choice/approval.
- `seed_plan(plan_id) -> None` creates durable requests for non-approval nodes of an approved plan, without launching before dependencies are accepted.
- `monitor_requests(project_id, cursor) -> list[dict]` returns pending plan-related choices and immutable policy/evidence context.
- `apply_monitor_selections(database, task, selections) -> None` runs in the same transaction as monitor findings/cursor acceptance; validate IDs actually supplied in that saved task prompt, current plan version, policy digest, and the persisted successful monitor task.
- `dispatch_ready() -> None` enqueues authorized requests atomically, respecting plan versions, graph readiness, holds, pauses, concurrency, policy freshness, and mode permissions.
- `start_check(task) -> dict` rechecks the saved worker grant before the runtime invokes a harness.
- `record_workspace(task, workspace) -> None` saves program-generated provenance against the current attempt token.
- `process_result(task) -> None` validates a terminal worker's structured report, exposes a candidate or failure, advances no successful graph node without acceptance, and marks processing atomically.

The existing Store enqueue/claim/heartbeat paths must reject worker jobs without a valid linked request and recheck execution gates.
Plan associations determine selection authority; caller-supplied role names cannot grant monitor authority.
Associate planning with its original user event so the same event cannot be relabeled as unrelated work to bypass routing ownership.
Language meaning still requires judgment; do not claim software can prove that arbitrary prose is unrelated.

## Runtime and transport integration

After processing role results, seed approved plans and dispatch eligible selected worker requests.
Include pending plan routing requests and program-captured policy/evidence in monitor task prompts.
Monitor output accepts an optional `worker_selections` array of `{request_id, choice}`; legacy findings-only responses remain valid.
Accept findings, holds, selections, cursor, and processed acknowledgment in one transaction.

Worker task config contains a `worker` object with request ID, concrete profile, mode, project root, and accepted dependency commits.
Core role configuration remains exactly orchestrator/planner/critic/monitor.
Worker runtime uses `roles/worker.md`, explicit executor settings and shared supervisor limits.
Its report is strict JSON `{summary: string, changes: [string], checks: [string], remaining_issues: [string]}`.
Store this alongside program-captured workspace/diff provenance.
Claims in the report do not independently prove tests passed.

Expose policy inspection, request/get/list, unrelated selection, and policy refresh through the common API/MCP/Pi bridge.
Approval, override, acceptance, and approval-node completion remain operator CLI actions.
Keep the native foreground tool guards: workers launch through the supervisor, not native untracked agent tools.
Update role instructions, documentation, the public explainer, and tests to distinguish enabled capabilities from an unconfigured project policy.
