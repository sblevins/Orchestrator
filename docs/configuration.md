# Configuration

`orchestrator.config.load_config(home, project_id=None)` returns a fresh validated dictionary.
It reads tracked `config/default.toml` relative to the installed source checkout, then optional `<home>/config/local.toml`, then optional `<home>/config/projects/<project_id>.toml`, then optional `<home>/config/projects/<project_id>.json`.
Tables merge recursively; scalar values and lists replace earlier values.
Validation applies to the final merged configuration.
Plain version: your private settings change the defaults, and project settings change them only for that project.
A shorter tool list removes permissions rather than adding to the previous list.

`validate_config(config)` returns `None` on success.
`role_config(config, role)` validates the configuration and returns an independent copy of the selected role settings.
All reported configuration errors use `ConfigurationError`, a subclass of `ValueError`.
Missing optional files are allowed; malformed files, invalid UTF-8, unreadable files, and missing tracked defaults are errors.
This source-checkout deployment requires the tracked `config` and `roles` directories alongside `orchestrator`.
Do not distribute only the Python package without those resources.

## Private overrides

Copy `config/local.example.toml` to `<home>/config/local.toml` for machine-wide preferences.
Copy `config/project.example.toml` to `<home>/config/projects/<project_id>.toml` for project-specific preferences.
Protect private files with mode `0600` and their containing directories with mode `0700`.
The loader does not create files, change permissions, or expand environment variables.
Project identifiers must be 1-128 ASCII letters, digits, underscores, or hyphens, beginning with a letter or digit.
Paths and file extensions are not project identifiers.
Private configuration symlinks must resolve within the selected home, including when their target does not exist.
Configuration is trusted local administrative input, not a sandbox for someone who can modify the home concurrently.
Plain version: use a simple project name, keep your private files private, and do not let other users change them.

## Conversational project settings

Read `project_settings` for effective settings, `revision`, and `can_configure`.
The active bound coordinator can call `configure_project` with a partial `settings` object and optional `expected_revision` repeatedly.
It recursively merges the patch, validates the effective configuration, and writes only private `<home>/config/projects/<bound-id>.json` with mode `0600` in private directories.
It never modifies global settings, `config/local.toml`, tracked defaults, another project's settings, or the project TOML override.
On a revision conflict, reread and reconcile rather than overwriting another change.
A `null` value removes only this project's own override for that setting, restoring the inherited value.
Unknown setting names are rejected with spelling guidance; shared-only `supervisor.max_parallel` and `supervisor.poll_seconds` are rejected in favor of `execution.max_parallel` and `config/local.toml`.
If a project's effective settings become invalid, only that project's background work pauses and a notification explains the error.
Its coordinator can still receive prompts and use the Orchestrator tools to inspect `project_settings` and repair it with `configure_project`, while other tools stay blocked until the repair.
Settings include roles and effort, personalization, monitoring, planning and named templates, execution, and permissions.
Arbitrary adapter/frontend executable changes are rejected conversationally because a replacement executable can affect things outside the project and ignore safety flags.
Existing tasks keep captured role, model, and specialist read-context settings.
Project permissions, worker enablement, and worker concurrency are live controls.
Native foreground model and effort changes still use `/model` and `/effort`.
Plain version: ask for a change here, and only this project's future work uses it.

For example, the `configure_project` payload can be:

```json
{
  "settings": {
    "roles": {"monitor": {"effort": "high"}},
    "execution": {"max_parallel": 3},
    "permissions": {
      "coordinator_approvals": true,
      "require_write_approval": false,
      "enforce_monitor_holds": true
    }
  }
}
```

Those three permission values are the defaults, and each must be a boolean.
`coordinator_approvals` permits the bound coordinator's approval APIs; disabling it does not prevent asking to re-enable it through `configure_project`.
`require_write_approval` adds explicit authorization for write workers when true; false does not remove explicit approval required by a routing rule.
`enforce_monitor_holds` controls whether unresolved monitor holds block progress.
Use `approve_plan(plan_id, reason)`, `resolve_hold(hold_id, reason)`, `approve_worker(request_id, reason)`, `accept_worker(request_id, reason)`, and `approve_node(plan_id, node_id, reason)` for authorized decisions in this project.
Use `cancel_worker(request_id, reason)` for a worker or entire comparison team.
Plan approval still requires independent review and fresh monitor evidence.
`execution.unattended = true` supplies standing project authorization for reviewed-plan approval and candidate acceptance only when `remaining_issues` is explicitly empty and live gates pass.
It never supplies policy-required explicit worker approval, including security-audit teams; only a team's parent request can be accepted.
Project ownership, dependency correctness, no blind retry of unknown outcomes, isolated source workspaces, and credential limits remain intrinsic boundaries.
Restricted workers have file tools by default; `commands.enabled = true` adds `run_command`, which is OS-sandboxed unless `commands.sandbox = false`.
Trusted worker commands are always unsandboxed.
Plain version: project permission settings do not allow skipping required work, and trusted commands need care because they can act outside the project.

## Specialist read context

Use bound `configure_project` to give planner, critic, and monitor access to explicitly selected reference directories, including sibling Git worktrees or another project chosen as reference.
The default is `context.read_roots = []`; no sibling or parent directory is discovered or authorized automatically.
For example:

```json
{"settings":{"context":{"read_roots":[{"alias":"design","path":"/absolute/path/to/design-worktree"}]}}}
```

Each entry has exactly `alias` and `path`; at most 16 entries are allowed.
Aliases are unique, 1-64 ASCII letters, digits, underscores or hyphens, starting with a letter or digit; `project` is reserved.
Directories must exist when explicitly saving `context.read_roots` and when launching a specialist; conversational input may use `~` or a directory alias, which is resolved to an absolute canonical path before saving.
Stored configuration requires canonical absolute path strings; duplicate directories, host-wide roots, the home directory, paths overlapping explicit authentication or private supervisor state/configuration, and paths inside Git, `.orchestrator` or host credential-store directories (`.ssh`, `.aws`, `.azure`, `.gnupg`, `.docker`, `.kube`) are rejected.
Other folder names such as `secrets`, `tokens` or `.claude` do not block a reference; the Pi broker still hides credential files and Git metadata inside it.
Launch rechecks availability and refuses a captured path that now resolves elsewhere, rather than following a retargeted symlink.
Choose the specific reference directory, not a parent containing unrelated projects.
Prompts list the captured aliases and absolute paths; use absolute tool paths for references, and resolve project-relative references against the registered project root.
These directories are mutable read context, not pinned Git snapshots; uncommitted design files can be inspected without changing project identity or history.
Neither write authority nor worker workspace roots change, and `execution.base_ref` remains solely a worker checkout setting.
Existing queued/running tasks and their normal planning-review chains keep their captured settings; an eligible explicit `retry_review` captures current settings without rerunning its saved planner.
The directory contents themselves are not frozen when settings are captured.
A missing or moved root fails only the affected specialist launch with a repair/remove message, not ordinary worker builders, settings inspection, or unrelated settings changes.
Repair or remove it with `configure_project`; `[]` disables additional roots and `null` removes the project override.
Plain version: name the extra folders specialists may read, and new work can inspect them without changing or editing either project.

## Supervisor and personalization

`supervisor` controls polling, heartbeat/stale thresholds, parallelism, monitor cadence/batching, and shared execution safeguards.
The shared execution fields are `task_timeout_seconds`, `task_memory`, `task_cpus`, `frontend_memory`, and `frontend_cpus`.
Defaults reserve 2G and one CPU per task/frontend, with a 900-second task deadline; there are no per-role resource settings.
Timing values are finite numbers from 0.1 through 86400 seconds; stale time must exceed heartbeat time.
Parallelism is an integer from 1 through 64, and monitor batches contain 1 through 10000 events.
Boolean values are not numbers.
`personalization.name` and `personalization.communication_style` are nonempty strings.
Defaults request concise, neutral, practical responses, independent evidence, no pirate language, and adherence to project-specific standing authorization without invented repeated approvals.
These preferences inform prompts; they do not replace adapter permission enforcement.

## Planning graphs and workflows

`planning.structure` is required and currently accepts only `"graph"`.
`planning.workflow` defaults to `"plan-review"` and selects a workflow template by name, not by path.
Names follow the same safe identifier rules as project identifiers: 1-128 ASCII letters, digits, underscores, or hyphens, beginning with a letter or digit.
`planning.max_review_rounds` defaults to 3 and accepts integers from 1 through 100, excluding booleans.
`planning.clarification` defaults to `material`, with `always` and `none` also accepted; role instructions should settle material unknowns before a paid planner call.
Reaching this limit does not mean the critic approved the plan.

Use `configure_project` to save project-local `planning.templates` in private `<home>/config/projects/<bound-id>.json` and `planning.workflow` to select one.
`planning.templates` optionally maps up to 64 safe names to complete plan graph objects with `summary`, `assumptions`, `risks`, `questions`, and `nodes`.
These inline templates are validated during configuration and take precedence over file templates of the same name.
Tracked templates live at `workflows/<name>.json`; shared private replacements live at `<home>/config/workflows/<name>.json` and take precedence over tracked files for that name.
The home workflow directory is administrator-only shared customization, never a project-scoped edit destination.
There are no configurable template-directory or arbitrary-path settings.
For file templates, the config loader validates the selected name only; the graph module owns loading, missing-template errors, graph validation, and path containment checks.
A valid configuration is not proof that a selected file template exists or is valid.
Protect private workflow templates with the same file permissions as private TOML overrides.
Plain version: ask in chat to save and select this project's task order without changing shared settings.
Shared workflow files are for administrator changes that may affect multiple projects.

The planner must return a JSON object with `summary` as a string, `assumptions`, `risks`, and `questions` as string lists, and `nodes` as a list of node objects.
Each node has string `id`, `title`, and `description` fields; string lists `depends_on` and `acceptance_criteria`; and `kind` equal to `work`, `review`, or `approval`.
An optional `mode` defaults to `read`; only work nodes can request `write`.
The graph must have unique identifiers, valid dependency references, and no cycles or self-dependencies.
The critic must return a JSON object with `verdict` equal to `approved` or `changes_requested`, and a `findings` list.
The monitor must return a JSON object with integer `reviewed_through` and a `findings` list whose entries have `severity` equal to `info`, `warning`, or `blocking`, string `summary`, and optional string `evidence` and `proposed_action`.
All roles put explanations inside their JSON, without surrounding prose or Markdown fences.
Prompts specify these response contracts, but the consuming program must validate responses independently.
Plain version: the roles return named fields the program can check, not just a paragraph saying the work is ready.

`execution.max_parallel` defaults to 3 and accepts integers from 1 through 64, excluding booleans.
It is the graph-execution concurrency limit, separate from `supervisor.max_parallel` for supervisor-managed role runs; one setting does not overwrite the other.
`execution.dependency_failure` defaults to `"block"` and also accepts `"cancel"`.
The runtime must enforce the chosen policy for dependent nodes when a prerequisite fails and must never treat a failed prerequisite as completed successfully.
It must allow a node to start only when its dependencies have actually succeeded and required approvals are present.
These preferences are enforced by the worker service; they do not grant approval by themselves.
Plain version: the program, not a model's promise, must prevent a task from starting too early.
If required work fails, tasks that need it stay blocked or are cancelled according to your setting.

## Roles and adapters

Exactly four core roles are configured: `orchestrator`, `planner`, `critic`, and `monitor`.
Each requires `adapter`, `model`, `effort`, and `allowed_tools`.
Timeout, CPU, memory, and dollar-budget parameters are not role settings.
An optional `prompt_path` must be exactly `roles/<role>.md` for that role.
Edit the tracked role Markdown files to change role instructions.
Every role's model and effort can be overridden privately.

The orchestrator defaults to `claude-sonnet-5-5` with low effort; planner and monitor default to `claude-opus-5-5` with high effort.
The critic defaults to `gpt-6-astra` through Pi with provider `openai-codex` and high effort.
Exact model identifiers are passed unchanged.
Claude Code also accepts case-insensitive family names `Opus`, `Sonnet`, `Haiku`, and `Fable`; other models use Pi with an explicit `provider` setting.
These requested identifiers are not a claim of provider availability; configure the exact identifier your provider supports.
Claude efforts are `low`, `medium`, `high`, `xhigh`, and `max`; Pi efforts are `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`.
Explicit efforts never silently fall back; `max-supported` is an intentional capability-based selector, not an effort downgrade.
Plain version: you can change any model name, but the chosen command must support the effort setting and the provider must actually offer that model.

The shared supervisor task timeout ranges from 1 through 86400 seconds, and shared task/frontend CPU reservations range from 1 through 64.
Per-role dollar budgets have been removed; neither adapter receives a spending-cap flag.
Remove `max_budget_usd`, `timeout_seconds`, `memory`, and `cpus` from older role overrides before validating them.
If customization of execution limits is needed, change the shared supervisor fields instead.
Memory/CPU reservations and task deadlines remain independent protections against resource exhaustion and stuck processes.
Memory uses positive integer `M` or `G` strings, bounded from `64M` through `1024G`, with `1G` equal to `1024M`.
Resource settings express requested reservations; the runtime must still obtain machine resources before launching work.
Allowed tools are a unique list containing only `Read`, `Glob`, and `Grep`.
Claude supports narrower lists, including an empty list.
Pi translates the same logical permissions into controlled file tools, without shell access.
Adapters must translate these logical read-only permissions into their own enforcement and reject unsupported restrictions rather than widening access.
Plain version: choose the model and how carefully it should think; the background program handles machine limits and checks what it may read.

`adapters.claude.command` defaults to `["claude"]`, and `adapters.pi.command` defaults to `["pi"]`.
Commands are argv lists containing exactly one nonempty executable name or path.
All additional arguments are rejected because the adapter owns sandbox, permission, and tool flags.
Known dangerous bypass spellings are also rejected in the executable entry.
Executable paths are trusted administrator selections; validation cannot prove an arbitrary executable is safe.
Specialist argv is not shell-interpolated, and native permission prompts are not automatically approved.

## Model families or exact versions

Set `model` to a family name to follow the harness-supported family release, or to a full model ID to request a particular version.
The supported bare families are `Opus`, `Sonnet`, `Haiku`, and `Fable`, plus Pi's `Astra` and `Sol`, in any letter case.
Pi resolves families within the selected provider's installed catalog, preferring an exact ID before family matching.
Pi worker profiles accept `max-supported` to select the highest supported effort for the chosen model; an explicitly requested unsupported effort still fails rather than silently falling back.
Core role settings and Claude workers still require a supported explicit effort.
No special `latest` setting or version table needs updating in this repository.
Existing exact defaults and private pins are not migrated automatically.

```toml
[roles.planner]
model = "Opus"
effort = "high"

[roles.monitor]
model = "Fable"
effort = "high"

# Or pin a version instead:
# [roles.planner]
# model = "claude-opus-5-5"
```

Claude execution passes the canonical native alias, for example `opus`, to Claude Code.
Claude selects its current provider/account-supported alias target; installed Claude version, organization restrictions, gateways, and `ANTHROPIC_DEFAULT_*_MODEL` overrides can affect that target.
Orchestrator does not claim an alias bypasses those controls or guarantees the newest release worldwide.
Keep Claude updated when you want new native alias targets, and use an exact ID when reproducibility matters.
The same aliases work in worker policy `model` fields with `harness: "claude"`.
Pi background executors still reject Anthropic families; choosing a family never changes the required execution harness.
Plain version: `Opus` follows Claude's supported Opus choice; `claude-opus-5-5` keeps requesting that named version.

When the foreground runs in Pi with an Anthropic orchestrator role, its owned extension chooses the newest stable family member in Pi's loaded `anthropic` catalog.
It compares version numbers numerically, so `4-10` is newer than `4-9`, and uses valid release-date suffixes to order snapshots within a version.
It excludes preview/context variants and unrelated providers, and rejects missing or equally ranked ambiguous matches rather than guessing.
A full exact catalog ID always wins, including an explicitly selected preview or custom ID.
Authentication failure stops startup rather than choosing an older model.
Pi's saved catalog can lag new releases; update its catalog or configure an available exact ID if necessary.
A family is selected once at startup; extension reload preserves subsequent manual model changes.
Starting a new process resolves the family again.
Plain version: Pi picks the newest matching model it knows, not whichever name happens to sort last.

Pi bootstrap entries record requested and resolved foreground IDs.
Claude task completion records `model_selection.requested_model`, `family`, and any `reported_models` from the harness's `modelUsage` metadata, available through `task` and detailed `worker_view`.
That usage list can contain internal/sub-agent models or resumed-session totals, so it is not presented as a single verified primary model.
Missing usage metadata means the actual models are unknown, not inferred from the alias.
This feature adds no paid model-resolution call and does not verify live model entitlement or provider-side remapping.

## Frontends and worker routing

`frontends.preferred` defaults to `claude` and also accepts `pi`.
`frontends.claude.command` and `frontends.pi.command` follow the same executable-only argv format.
An executable launcher script is valid, for example `["/absolute/path/to/pi-launcher"]`; an interpreter-plus-script list or extra command-line flags is not.
Launcher scripts are trusted local programs, not an escape from specialist read-only restrictions.
Pi supports both the interactive frontend and supervised specialist execution.
Claude remains the preferred interactive frontend, with native voice handled by Claude itself.
Both interfaces can start directly in the repository without the optional launcher.
Native Pi applies `roles.orchestrator`; native Claude foreground selection follows its own project/user/CLI precedence, so a global CLI pin can override the project default.
Use native `/model` and `/effort`, or the optional launcher, to select the configured fast Claude role explicitly.

`workers` contains only a boolean `enabled` setting, defaulting to true.
`routing.enabled` also defaults to true, but neither setting supplies worker profiles.
`routing.rules` and `routing.first_mate` remain empty compatibility fields, not active inline policy.
Set worker preferences in `<project-root>/.orchestrator/crew-dispatch.json`, falling back to `<home>/config/crew-dispatch.json` only when the project file is absent.
Invalid project policy is an error, never permission to use a fallback.
Routing can be created, replaced, or repaired repeatedly through `project_setup` and `setup_project(policy, expected_revision)`, with optional revision checking.
Deleted policies and incomplete drafts do not permanently close configuration or require an operator CLI handoff.
See [project routing](project-routing.md) for classifications, difficulty mapping, comparison teams, legacy rules/default, revision checks, and conversational approvals.
Plain version: both interfaces share the same workers, but nothing runs until you configure who may do each task.

## Execution and image generation

`execution.mode` is `restricted` (the default) or `trusted`.
Restricted mode retains the coordinator's native-tool restrictions and file-only workers unless optional owned tools such as `commands.enabled` are enabled.
Trusted mode enables native foreground tools and the owned worker `run_command` tool for build and test commands; these commands are not OS-sandboxed.
`execution.base_ref` selects a Git branch to resolve and freeze for worker checkout preparation, not a directory.
It does not automatically merge or push candidate changes.
Plain version: trusted tools can run commands, and the branch setting chooses the code workers start from.

`commands.enabled` can separately enable the owned command tool without enabling trusted native foreground tools.
Command settings include sandboxing, network access, and timeouts.
`execution.mode` controls native foreground trust and makes worker commands available; `commands.sandbox` independently controls whether explicitly enabled restricted-mode worker commands use the OS sandbox.
Trusted mode always forces unsandboxed worker commands, and the default restricted command setting stays sandboxed.
Plain version: trusted mode, or `commands.sandbox = false`, lets worker commands do anything your user account can; otherwise enabled worker commands stay in a sandbox.

Project `images.enabled` defaults to false.
Explicit image generation uses the separately billed OpenAI Images API with `OPENAI_API_KEY`, not subscription OAuth credentials.
Supported outputs are PNG and JPEG, written through the owned image tool into its authorized workspace.
Do not infer live image access or paid verification from offline helper tests.
Plain version: enable images separately, provide an API key, and expect a separate API charge.

## Monitor intake

`monitoring.review_every_prompt = true` requests review for every prompt.
By default it is false, and only exact normalized entries in `monitoring.routine_prompts` avoid waking a review.
Normalization ignores case, surrounding/repeated whitespace, and trailing question marks, periods, or exclamation marks.
Combined instructions and unknown messages are reviewed.
All prompts remain saved regardless of this classification; quiet prompts remain available in later review evidence.
Prompts saved before project selection are mirrored into that project when the instance binds, with duplicate mirroring prevented by prompt identity.
The monitor waits for foreground completion and `monitoring.quiet_seconds`, which defaults to 20.
Claude Code runs no hook when the user interrupts a response, so an interrupted turn never reports completion.
After `monitoring.foreground_stale_seconds` (900 by default, 60 to 86400) without activity, an unfinished turn permits silent background monitor scheduling, which also supplies the monitor evidence plan approval needs.
Claude activity is prompt and tool-use hooks.
Pi refreshes activity every 30 seconds while its agent is busy, including during one long tool, and stops when Pi reports the agent idle.
The tradeoff is that a Claude response running that long without any tool call or tool completion can be reviewed in the background while it is still running.
Interrupting delivery never relies on this limit: it still waits for a completed turn or an exited frontend.
Plain version: if a turn looks abandoned for 15 minutes, background checks start again, but nothing interrupts the user until the turn really ends.
Only blocking monitor findings interject; informational and warning findings remain silent but inspectable and acknowledgeable.
Plain version: a simple status question does not need another model call, and routine feedback does not interrupt the conversation.
