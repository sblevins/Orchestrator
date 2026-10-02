# Workers and routing

Workers and routing are enabled in tracked configuration, but **there are no worker model defaults**.
Workers are tracked background jobs, not new Herder tabs or windows.
Durable records remain in Orchestrator tools; frontend-specific observers also represent jobs and team children in native sub-agent interfaces.
Claude uses Haiku watchers and Pi uses local no-LLM plugin observers.
Stopping an observer does not cancel the worker, and Pi FleetView/live partial text remain limited.
See [worker visibility](worker-visibility.md) for attachment behavior and [project routing](project-routing.md) for the current policy schema, difficulty mappings, teams, and conversational APIs.

The bound coordinator selects both planned and on-demand work without changing plan origins.
The monitor still selects supplied pending legacy plan requests.
Classification suggestions become `worker.routing_recommended` notifications, not executable selections or failed monitor reviews; the coordinator chooses final difficulty or effort.
Explicit policy-match overrides remain a separate operator CLI capability, not a caller-supplied role label.
Plain version: workers follow your saved policy, and the coordinator makes the final classification choice here in chat.

## Configure and select

Register and bind the project, then use `project_setup` and `setup_project` whenever routing needs creating, replacing, or repairing.
There is no permanent initial-setup seal or required CLI handoff, including after deletion or an incomplete draft.
The active coordinator saves only the bound project's `.orchestrator/crew-dispatch.json` with optional `expected_revision` checking.
Saving a policy does not execute or accept work, and observers cannot edit it.
Valid incomplete drafts can be saved with readiness blockers; invalid schema is rejected and validation does not prove live model access.
Manual editors do not share the database lock, so do not edit the file concurrently with a conversational save.
The program never commits or stashes the policy, edits `.gitignore`, or changes Git's exclude settings for you.

Manual setup remains optional:

```bash
bin/orchestrator routing init --project PROJECT_ID
bin/orchestrator routing show --project PROJECT_ID
bin/orchestrator routing validate --project PROJECT_ID
```

`routing init` creates an empty `{"rules": []}` placeholder without overwriting an existing file.
The project policy takes precedence over the home fallback; invalid project policy does not silently fall back.
Use classifications for a configured profile or comparison team, or legacy natural-language `rules` and `default` for single-profile selection.
Do not confuse legacy quota-dependent candidate arrays with comparison teams.
Quota arrays, floors, and quota-balanced choices remain blocked without trusted sanitized quota evidence; caller-provided quota numbers cannot unblock them.
Claude Code executes Anthropic models; Pi requires an explicit non-Anthropic provider.
Direct Codex CLI execution is unsupported, and unsupported models or effort never silently fall back.
See [adapters](adapters.md) for authentication and file-tool controls.

## Selection and launch boundaries

Worker requests retain their user-message origin, policy digest, uncertainty, generation, concrete selection, and task link.
Plan requests also retain the immutable plan version and node identity; do not relabel them as unrelated work.
`select_worker` automatically refreshes an unattempted request to the current policy and clears prior approval when reselecting.
`refresh_worker_policy` remains an explicit way to clear a not-running request's stale choice and approval.
Queued and running selections capture their policy; later policy edits do not revoke or replay those jobs.
Attempted requests cannot be blindly refreshed, reselected, or replayed, and unknown outcomes remain visible for inspection.
Live permissions, concurrency, and worker/routing disable settings still apply at their enforcement points; role/model settings remain captured for existing tasks.
Plain version: update choices before work starts, without silently changing the model or repeating a job already started.

Permissions default to `coordinator_approvals=true`, `require_write_approval=false`, and `enforce_monitor_holds=true`, configurable locally through `configure_project`.
Authorized decisions use bound `approve_plan`, `resolve_hold`, `approve_worker`, `approve_node`, and `accept_worker` with target IDs and reasons; CLI commands remain optional.
Plan approval still requires independent review and fresh monitor evidence.
A rule marked `approval: "captain"` or an explicit override requires separate approval, even when the general write-approval preference is disabled.
Approval-only graph nodes require accepted prerequisites and cannot launch workers.
Project ownership, dependency correctness, source isolation, credential limits, and no blind retry of unknown outcomes remain mandatory regardless of optional gates.

An explicit policy-match override can be supplied through the CLI:

```bash
bin/orchestrator override-worker REQUEST_ID --choice-file /path/to/private-choice.json
```

The choice file must be a private regular file owned by the operator, with no group or other permissions.
Override selection does not grant execution approval or bypass executor validation.
For routine project approvals, use the conversational APIs rather than requiring an operator terminal.

## Workspaces, results, and acceptance

Read workers use file-reading tools only.
Accepted dependency commits are supplied through an isolated checkout when needed, and comparison teams freeze the same Git commit and accepted inputs across two rounds.
Write workers receive a separate worktree and branch created by the supervisor, never permission to edit the source checkout.
Snapshot preparation requires clean source state apart from the sole regular `.orchestrator/crew-dispatch.json` policy path.
That exception includes untracked files and tracked or staged additions, modifications, and deletions of the policy.
Other untracked files, real source changes, and unsafe policy paths still block isolated snapshots.
No stash, commit, ignore rule, or Git exclude write is needed to preserve routing-only edits.
Accepted prerequisite commits can be merged into the isolated worker checkout; this never integrates a candidate into the source branch.
Unsupported Git filters, merge drivers, unsafe paths, or provenance changes block preparation or capture.

Worker tools allow reading and permitted file edits only, with no shell commands or tests.
The supervisor prepares Git worktrees and captures bounded diffs and signed local candidate commits.
Signing and signature verification must already be configured; the supervisor never changes identity or disables signing.
These controls are not an operating-system sandbox against hostile programs running as the same user.
No worker automatically merges into the source branch, pushes, or opens a tab or window.
Plain version: workers prepare changes separately, without changing your current checkout or hiding unfinished source edits.

A successful report contains exactly `summary`, `changes`, `checks`, and `remaining_issues`, with the last three fields arrays of text.
Reports must disclose checks the worker could not perform rather than claiming tests ran without a test tool.
Successful execution creates a `candidate`; a plan node becomes `awaiting_review`, not `completed`.
Review the saved report, diff, commit, remaining issues, and independent verification before calling `accept_worker(request_id, reason)`.
For comparison teams, review the combined evidence and accept only the parent request, not individual children.
Acceptance rechecks applicable gates and completes the node, allowing dependent work to proceed.
Acknowledging a notification is not acceptance, and acceptance does not merge or push.
Use `cancel_worker(request_id, reason)` for a worker or an entire team and wait for terminal confirmation.
Plain version: check the result before accepting it, and only then let later jobs rely on it.
