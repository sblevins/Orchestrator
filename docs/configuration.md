# Configuration

`orchestrator.config.load_config(home, project_id=None)` returns a fresh validated dictionary.
It reads tracked `config/default.toml` relative to the installed source checkout, then optional `<home>/config/local.toml`, then optional `<home>/config/projects/<project_id>.toml`.
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

## Supervisor and personalization

`supervisor` controls polling, heartbeat/stale thresholds, parallelism, monitor cadence/batching, and shared execution safeguards.
The shared execution fields are `task_timeout_seconds`, `task_memory`, `task_cpus`, `frontend_memory`, and `frontend_cpus`.
Defaults reserve 2G and one CPU per task/frontend, with a 900-second task deadline; there are no per-role resource settings.
Timing values are finite numbers from 0.1 through 86400 seconds; stale time must exceed heartbeat time.
Parallelism is an integer from 1 through 64, and monitor batches contain 1 through 10000 events.
Boolean values are not numbers.
`personalization.name` and `personalization.communication_style` are nonempty strings.
Defaults request concise, neutral, practical responses, independent evidence, no pirate language, and no automatic approval of code execution or permission requests.
These preferences inform prompts; they do not replace adapter permission enforcement.

## Planning graphs and workflows

`planning.structure` is required and currently accepts only `"graph"`.
`planning.workflow` defaults to `"plan-review"` and selects a workflow template by name, not by path.
Names follow the same safe identifier rules as project identifiers: 1-128 ASCII letters, digits, underscores, or hyphens, beginning with a letter or digit.
`planning.max_review_rounds` defaults to 3 and accepts integers from 1 through 100, excluding booleans.
Reaching this limit does not mean the critic approved the plan.

Tracked templates live at `workflows/<name>.json`; private replacements live at `<home>/config/workflows/<name>.json` and take precedence for that name.
Select the name in local or project TOML to customize the workflow.
There are no configurable template-directory or arbitrary-path settings.
The config loader validates the selected name only; the graph module owns template loading, missing-template errors, template validation, and path containment checks.
A valid configuration is not proof that a template exists or that its graph is valid.
Protect private workflow templates with the same file permissions as private TOML overrides.
Plain version: choose a workflow by its short name, and put your customized version in the private workflow folder.
The program must check that file before using it.

The planner must return a JSON object with `summary` as a string, `assumptions`, `risks`, and `questions` as string lists, and `nodes` as a list of node objects.
Each node has string `id`, `title`, and `description` fields; string lists `depends_on` and `acceptance_criteria`; and `kind` equal to `work`, `review`, or `approval`.
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
These settings do not implement a scheduler, validate graphs, approve execution, or enable workers.
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
The critic defaults to `gpt-6-astra` through Codex with high effort.
Model identifiers are arbitrary nonempty strings passed unchanged to adapters, not a fixed model catalog.
These requested identifiers are not a claim of provider availability; configure the exact identifier your provider supports.
Claude efforts are `low`, `medium`, `high`, `xhigh`, and `max`; Codex efforts are `minimal`, `low`, `medium`, `high`, and `xhigh`.
No model-specific effort assumptions or silent fallbacks are applied.
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
Codex specialists require the full list because the CLI provides a read-only sandbox rather than individual Read/Glob/Grep switches; unsupported narrower permissions are rejected.
Adapters must translate these logical read-only permissions into their own enforcement and reject unsupported restrictions rather than widening access.
Plain version: choose the model and how carefully it should think; the background program handles machine limits and checks what it may read.

`adapters.claude.command` defaults to `["claude"]`, and `adapters.codex.command` defaults to `["codex"]`.
Commands are argv lists containing exactly one nonempty executable name or path.
All additional arguments are rejected because the adapter owns sandbox, permission, and tool flags.
Known dangerous bypass spellings are also rejected in the executable entry.
Executable paths are trusted administrator selections; validation cannot prove an arbitrary executable is safe.
There is no shell interpolation and no automatic approval setting.

## Frontends and deferred routing

`frontends.preferred` defaults to `claude` and also accepts `pi`.
`frontends.claude.command` and `frontends.pi.command` follow the same executable-only argv format.
An executable launcher script is valid, for example `["/absolute/path/to/pi-launcher"]`; an interpreter-plus-script list or extra command-line flags is not.
Launcher scripts are trusted local programs, not an escape from specialist read-only restrictions.
Pi is an interactive frontend, not a supported specialist adapter.
Claude remains the preferred interactive frontend, with native voice handled by Claude itself.
Both interfaces can start directly in the repository without the optional launcher.
Native Pi applies `roles.orchestrator`; native Claude foreground selection follows its own project/user/CLI precedence, so a global CLI pin can override the project default.
Use native `/model` and `/effort`, or the optional launcher, to select the configured fast Claude role explicitly.

`workers` must contain only `enabled = false`.
`routing.enabled` must be false, `routing.rules` must be an empty list, and optional `routing.first_mate` must be an empty table.
There is no default worker model, no executable First Mate policy, and no worker launch permission.
Attempts to enable workers or routing, add routing rules, or populate the reserved policy table fail validation.
Plain version: both chat interfaces may use the same supervisor, but temporary workers cannot run until a real router is implemented.

## Monitor intake

`monitoring.review_every_prompt = true` requests review for every prompt.
By default it is false, and only exact normalized entries in `monitoring.routine_prompts` avoid waking a review.
Normalization ignores case, surrounding/repeated whitespace, and trailing question marks, periods, or exclamation marks.
Combined instructions and unknown messages are reviewed.
All prompts remain saved regardless of this classification; quiet prompts remain available in later review evidence.
Prompts saved before project selection are mirrored into that project when the instance binds, with duplicate mirroring prevented by prompt identity.
Plain version: a simple status question does not need another model call, but changing the work does.
