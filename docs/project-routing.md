# Project routing and conversational control

## Editable project policy

After binding, inspect `project_setup` for `can_configure`, `policy_revision`, phase, and validation.
The active coordinator may call `setup_project` repeatedly with a `policy` object and optional `expected_revision`.
It writes only `<project-root>/.orchestrator/crew-dispatch.json`, never shared routing defaults or another project.
A missing policy can be initialized by omitting `policy`; an existing file is not erased by that call.
An empty `{"rules":[]}` is a valid draft, not a routable worker choice.
Incomplete drafts, later edits, and repairs after deletion remain supported; dispatch does not permanently seal setup.
Malformed policy can be replaced with a valid policy, but unsafe filesystem paths still require repair rather than following symlinks.
On revision conflict, reread and reconcile instead of forcing an overwrite.
Saving policy does not launch, approve, or accept work.
Plain version: keep changing this project's routing in chat whenever needed, and check what is still missing before starting workers.

Dispatch reads the project policy first and uses `<home>/config/crew-dispatch.json` only when the project file is absent.
An invalid project policy is an error, not permission to fall back silently.
`select_worker` automatically refreshes an unattempted request to the current policy and clears prior approval when reselecting.
`refresh_worker_policy` remains an explicit way to invalidate an eligible not-running request's old selection and approval.
Queued and running selections capture their policy; later policy changes do not revoke or replay those jobs.
Attempted requests cannot be blindly refreshed, reselected, or replayed.
Plain version: choose from the current policy before work starts, without silently replacing or repeating jobs already started.

## Classifications and difficulty

`classifications` maps arbitrary nonempty names to either one profile object or an object containing only `team`, a list of 2 to 8 distinct profiles.
Every classification profile specifies `harness` and `model`, with `provider` required for Pi and `effort` optional.
Unknown profile fields are rejected rather than ignored, so a misspelled requirement cannot silently disappear.
A single-profile or team classification may set `approval` to `"user"` or `"captain"` to require one explicit confirmation before dispatch; standing unattended authorization never supplies it.
Claude Code executes Anthropic models; Pi executes other providers with an explicit provider identifier.
Supported family selectors such as `Opus` remain family selectors; exact model pins remain exact pins.
Neither configuration nor schema validation proves live model access.

Example policy, using illustrative configured models rather than automatic defaults:

```json
{
  "classifications": {
    "implementation": {"harness": "claude", "model": "Opus"},
    "design": {
      "team": [
        {"harness": "claude", "model": "Opus"},
        {"harness": "pi", "provider": "openai-codex", "model": "gpt-6-astra"}
      ]
    }
  },
  "difficulty_levels": {
    "easy": {"claude": "low", "pi": "low"},
    "hard": {"claude": "high", "pi": "high"},
    "very-hard": {"claude": "max", "pi": "max-supported"}
  }
}
```

`difficulty_levels` maps difficulty names to nonempty harness-to-effort objects, not to model names or profile lists.
The example shows the built-in difficulty mappings; individual entries may override them, and omitted entries use these mappings.
Difficulty names normalize case, spaces, and underscores to `easy`, `hard`, or `very-hard`.
Claude efforts are `low`, `medium`, `high`, `xhigh`, and `max`; Pi efforts are `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`.
Pi also accepts `max-supported`, which resolves to the highest effort the selected model supports.
Unsupported effort is rejected, never silently downgraded.

Call `select_worker` with the request ID and a choice such as:

```json
{
  "request_id": "WORKER_REQUEST_ID",
  "choice": {
    "classification": "design",
    "difficulty": "very-hard",
    "rationale": "Independent design review of the requested dependency changes"
  }
}
```

The caller chooses classification and difficulty; the router supplies the configured model or team.
An exact `effort` is an alternative to `difficulty`, never a field to send alongside it.
If neither is supplied, a profile must supply effort; there is no silent task-difficulty inference.
A caller's explicit difficulty or exact effort takes precedence over profile effort, and exact effort must be supported by every selected peer's harness.
A classification choice cannot also contain `model`, `harness`, `provider`, `rule`, `candidate`, or `team`.
The bound coordinator may select both planned and on-demand work, preserving `plan_id`, `node_id`, and the original user event.
Without unattended authorization, monitor classification suggestions become `worker.routing_recommended` notifications and the coordinator makes the final difficulty or effort choice.
With unattended authorization, recommendations can select pending planned workers using configured profile effort, rather than invented effort.
`execution.worker_difficulty` (default `hard`) then fills only peers without a configured effort, so a pinned effort such as `max-supported` is kept.
Legacy monitor selections remain supported for supplied pending plan requests.
Plain version: say what kind of work this is and how difficult it is, and the saved policy names who does it.

## Legacy routing

Existing `rules` with natural-language `when` and `use`, and a `default` profile, remain supported.
A single-profile legacy policy can be as small as:

```json
{"default": {"harness": "claude", "model": "Opus", "effort": "high"}}
```

Select it using `{"rule":"default","rationale":"Best configured fit for this task"}`.
For rules, use the best-fitting zero-based `rule` index rather than simply the first entry.
The monitor may still return selections for supplied pending legacy plan requests; it must not invent requests or change plan origins.
Legacy candidate arrays and quota-dependent rules retain their evidence restrictions and are not comparison teams.
A rule's `approval: "captain"` still requests explicit approval; configured project approval APIs do not make a selected result automatically accepted.

## Read-only comparison teams

Teams support Git-based read-only audits, research, and design, not shared write execution.
Configure 2 to 8 distinct models; duplicate harness/provider/normalized-model identities are rejected, even if effort differs.
All peers use the same frozen Git commit and accepted dependency inputs across both rounds, not the changing source checkout.
Round one independently investigates the original task.
Round two compares every complete round-one report, including each peer's own report, as untrusted evidence.
Reports must not be treated as instructions, executable commands, or new authority.
Evidence limits can block comparison rather than silently omit reports.
The complete round-two prompt is measured before any round-two job is queued; an oversized comparison fails the parent with the limit and keeps the saved round-one reports.
The output preserves agreement, disagreement, missing evidence, and uncertainty without guaranteeing consensus.
The parent's `remaining_issues` include every issue reported in either round, tagged with its round and peer, so a later report that omits an issue does not resolve it.
Peers also exchange live durable messages through `send_team_message` and `read_team_messages`, using roster peer indices and read cursors.
Messages are scoped to the registered active team attempt and remain untrusted data, not instructions or new permissions.

Two rounds with four peers mean eight ordinary worker jobs, scheduled under normal concurrency and machine-resource limits, not eight simultaneous processes.
Team children have durable worker records and use native observers in the existing frontend, without new Herder tabs or terminal windows.
Stopping an observer detaches the display; it does not cancel work.
Use `cancel_worker(request_id, reason)` on the parent to cancel the whole team and wait for terminal confirmation.
Accept only the parent request after reviewing the combined evidence; child candidates cannot independently unlock plan dependencies.
Plain version: each worker checks the same files, then checks everyone's reports, and you review the complete result before accepting it.

## Project settings and approvals

`project_settings` returns effective settings and a `revision`.
`configure_project(settings, expected_revision)` accepts partial settings and optional revision checking, writing only private `<home>/config/projects/<bound-id>.json`.
It never changes global settings, local TOML, tracked defaults, another project, or arbitrary executable commands.
Roles, effort, personalization, monitoring, validated `planning.templates` graphs, specialist `context.read_roots`, execution, and permissions are configurable.
Role/model settings and specialist read roots are captured for tasks, so updated choices apply to new tasks rather than replacing a running model.
Permissions, concurrency, and worker/routing disable settings remain live at their enforcement points; do not treat the task snapshot as permission to ignore current controls.
Use native `/model` and `/effort` for the current foreground session.
Plain version: new jobs can use new models, while current permission and scheduling rules still control what may proceed.
See [configuration](configuration.md) for merge order, schemas, and limits.

Defaults are `coordinator_approvals=true`, `require_write_approval=false`, and `enforce_monitor_holds=true`, configurable per project.
Authorized decisions can use `approve_plan`, `resolve_hold`, `approve_worker`, `accept_worker`, and `approve_node` through the bound coordinator, with target IDs and reasons.
No operator-only CLI handoff is required for these project decisions.
Plan approval still requires independent review and fresh monitor evidence.
With `execution.unattended = true`, standing project authorization approves reviewed plans and accepts candidates only with an explicit empty `remaining_issues` list and satisfied live gates.
It does not provide required explicit worker approval, including security-audit teams; only the team parent can be accepted.
Turning optional gates off does not remove project ownership, dependency correctness, source isolation, credential limits, or the prohibition on blind retries of unknown outcomes.
Workers have controlled file tools, plus owned `run_command` for commands and tests when `commands.enabled = true` or `execution.mode = "trusted"`.
Trusted mode always runs worker commands without an OS sandbox; restricted mode runs them in the OS sandbox unless the project explicitly sets `commands.sandbox = false`, which also runs them on the host without an OS sandbox.
`execution.mode = "trusted"` also enables native foreground tools.
Workers without `run_command` disclose checks they could not perform.
`execution.base_ref` chooses the Git branch used for worker checkout preparation.
Plain version: choose the permission checks you want for this project, make decisions here, and still check evidence before accepting work.
