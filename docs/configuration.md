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

`supervisor` requires `poll_seconds`, `heartbeat_seconds`, `stale_seconds`, `max_parallel`, `monitor_interval_seconds`, and `monitor_batch_events`.
Timing values are finite numbers from 0.1 through 86400 seconds; stale time must exceed heartbeat time.
Parallelism is an integer from 1 through 64, and monitor batches contain 1 through 10000 events.
Boolean values are not numbers.
`personalization.name` and `personalization.communication_style` are nonempty strings.
Defaults request concise, neutral, practical responses, independent evidence, no pirate language, and no automatic approval of code execution or permission requests.
These preferences inform prompts; they do not replace adapter permission enforcement.

## Roles and adapters

Exactly four core roles are configured: `orchestrator`, `planner`, `critic`, and `monitor`.
Each requires `adapter`, `model`, `effort`, `timeout_seconds`, `max_budget_usd`, `memory`, `cpus`, and `allowed_tools`.
An optional `prompt_path` must be exactly `roles/<role>.md` for that role.
Edit the tracked role Markdown files to change role instructions.
Every role's model and resource settings can be overridden privately.

The orchestrator defaults to `claude-sonnet-5-5` with low effort; planner and monitor default to `claude-opus-5-5` with high effort.
The critic defaults to `gpt-6-astra` through Codex with high effort.
Model identifiers are arbitrary nonempty strings passed unchanged to adapters, not a fixed model catalog.
These requested identifiers are not a claim of provider availability; configure the exact identifier your provider supports.
Claude efforts are `low`, `medium`, and `high`; Codex efforts are `minimal`, `low`, `medium`, `high`, and `xhigh`.
No model-specific effort assumptions or silent fallbacks are applied.
Plain version: you can change any model name, but the chosen command must support the effort setting and the provider must actually offer that model.

Timeouts range from 1 through 86400 seconds, budgets from 0.01 through 1000 USD, and integer CPU reservations from 1 through 64.
Memory uses positive integer `M` or `G` strings, bounded from `64M` through `1024G`, with `1G` equal to `1024M`.
Resource settings express requested reservations; the runtime must still obtain machine resources before launching work.
Allowed tools are a unique list containing only `Read`, `Glob`, and `Grep`; an empty list is valid.
Adapters must translate these logical read-only permissions into their own enforcement and reject unsupported restrictions rather than widening access.
Plain version: configuration says what a specialist may read and how much it may use; the launcher must enforce those limits.

`adapters.claude.command` defaults to `["claude"]`, and `adapters.codex.command` defaults to `["codex"]`.
Commands are argv lists containing exactly one nonempty executable name or path.
All additional arguments are rejected because the adapter owns sandbox, permission, and tool flags.
Known dangerous bypass spellings are also rejected in the executable entry.
Executable paths are trusted administrator selections; validation cannot prove an arbitrary executable is safe.
There is no shell interpolation and no automatic approval setting.

## Frontends and deferred routing

`frontends.preferred` defaults to `claude` and also accepts `pi`.
`frontends.claude.command` and `frontends.pi.command` follow the same executable-only argv format.
Pi is an interactive frontend, not a supported specialist adapter.
Claude remains the preferred interactive frontend, with native voice handled by Claude itself.

`workers` must contain only `enabled = false`.
`routing.enabled` must be false, `routing.rules` must be an empty list, and optional `routing.first_mate` must be an empty table.
There is no default worker model, no executable First Mate policy, and no worker launch permission.
Attempts to enable workers or routing, add routing rules, or populate the reserved policy table fail validation.
Plain version: both chat interfaces may use the same supervisor, but temporary workers cannot run until a real router is implemented.
