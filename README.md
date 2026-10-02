# Orchestrator

**[Read the visual explainer](https://sblevins.github.io/Orchestrator/explainer/)** · [Open the HTML locally](docs/explainer/index.html) · [Default settings](config/default.toml)

A neutral, project-scoped coordinator with native **Claude Code and Pi** interfaces.
Inspired by [FirstMate](https://github.com/kunchenguid/firstmate), especially its persistent project coordination, durable task tracking, and background supervision.
This is an independent implementation, not an official FirstMate project.
Claude Code is preferred so its own voice interface remains available.
The fast orchestrator talks with you; a planner develops graph-based plans, an independent critic checks them, and a slow monitor reviews important decisions in the background.

The durable supervisor is a **program**, not a model.
It owns task state, dependencies, process identities, event delivery, notes, and review gates in SQLite.
Plain version: closing a chat does not erase who is working or what still needs attention.

## Start

Requirements: Linux, Python 3.11+, `machine-resources`, authenticated Claude Code and Pi for the default specialist roles.
No Python runtime dependencies need installing.

```bash
cd ~/Agents/Orchestrator
claude
# Or:
pi
```

The repository hooks/extension initialize the coordinator automatically.
Approve native project trust and the local Orchestrator integration when prompted; there is no separate launcher requirement.
Tell the coordinator which project to work on and its existing directory.
It can register the project, bind this instance, and retrieve that project's notes and status.
Alternatively, register and select it yourself:

```bash
./bin/orchestrator project add my-project ~/Code/my-project
./bin/orchestrator start --frontend claude --project my-project
```

Open another terminal and start another instance for a different project.
Use the optional `bin/orchestrator start` launcher for explicit `--observer`, `--takeover`, or `--project` options.
Normal native resume restores the project binding without taking over another active instance.
A displaced instance must not silently reclaim authority.

**Foreground model:** native Pi selects `roles.orchestrator` automatically.
Native Claude respects its own settings; this machine's global wrapper currently pins Opus/high and overrides the project's Sonnet/low default.
Use Claude's `/model` and `/effort` to change that session, or the optional launcher to apply the configured foreground role explicitly.
Background roles always use Orchestrator configuration.

## Configure

Tracked defaults live in `config/default.toml`.
Private `config/local.toml` overrides them; `config/projects/PROJECT_ID.toml` adds project preferences, followed by private `config/projects/PROJECT_ID.json`.
Ask the bound coordinator to read `project_settings` and apply partial changes with `configure_project`, optionally checking `expected_revision`.
It changes only that project's JSON override, never shared defaults, local/global settings, or another project.
Roles, effort, personalization, monitoring, validated `planning.templates` graphs, execution, and permissions can be changed conversationally.
Role/model settings are captured for tasks, so new tasks use updated choices; permissions, concurrency, and worker/routing disable settings remain live at enforcement points.
Native foreground model and effort changes still use `/model` and `/effort`.
Executable commands remain trusted local configuration, not conversational settings, because they can have effects outside the project.
If you have no private configuration yet, run `./bin/orchestrator config init`.
Edit each role's model and effort without changing Python code.
Use `model = "Opus"` or `model = "Fable"` to follow that supported family, or an exact ID such as `claude-opus-5-5` to keep a version pin.
`Sonnet` and `Haiku` also work; Pi additionally supports `Astra` and `Sol` in the selected provider's installed catalog, with exact IDs taking precedence.
Family names are case-insensitive and existing pins stay unchanged.
Pi worker profiles can use `max-supported` to request the highest supported effort for the selected model.
See [model families](docs/configuration.md#model-families-or-exact-versions) for Claude/Pi resolution and catalog limits.
Shared background settings handle deadlines and machine-resource reservations; roles do not need individual timeout, CPU, or memory parameters.
There is no per-role dollar budget or spending-cap parameter.
Model identifiers are requests, not a guarantee of account access or measured superiority.

The agreed defaults are included in [`config/default.toml`](config/default.toml), not just in the explainer:

| Role | Adapter | Model | Effort |
| --- | --- | --- | --- |
| Fast orchestrator | Claude | `claude-sonnet-5-5` | `low` |
| Planner | Claude | `claude-opus-5-5` | `high` |
| Independent critic | Pi (`openai-codex`) | `gpt-6-astra` | `high` |
| Slow monitor | Claude | `claude-opus-5-5` | `high` |

Workers use a project-local routing policy with configured classifications or legacy FirstMate-compatible rules/default.
There is no default worker model; the caller chooses difficulty or exact effort, or uses an explicitly configured profile effort.
Claude Code runs Anthropic specialists; Pi runs specialists from every other provider.
Missing model access, unsupported effort, or a missing policy stops dispatch rather than silently switching models.
Fable and the other models discussed in the research remain alternatives, not silently enabled defaults.
Native Claude foreground selection still follows its own settings and CLI precedence; background roles and native Pi use the configured roles.

```toml
[personalization]
name = "Your name"
communication_style = "Concise and practical. Explain complicated points in plain English."

[monitoring]
review_every_prompt = false

[roles.monitor]
effort = "high"

[planning]
workflow = "plan-review"
max_review_rounds = 3

[execution]
max_parallel = 3
dependency_failure = "block"
```

Run `./bin/orchestrator config validate` after edits.
See [configuration](docs/configuration.md) and [workflow templates](docs/workflows.md) for all supported settings.
Every prompt is saved; exact routine status questions do not start an expensive review by default.
Ambiguous or consequential messages do, and the orchestrator cannot veto that requirement.

## Planning, monitoring, and images

Clarify material unknowns before a paid planner call, then obtain independent final review.
Use `retry_review` to retry an eligible failed critic against the saved draft without rerunning the planner; unknown outcomes are not blindly replayed.
The monitor waits for foreground completion and `monitoring.quiet_seconds` (20 by default).
Only blocking monitor findings interject; informational and warning findings stay silent.
Explicit image generation uses the separately billed OpenAI Images API with `OPENAI_API_KEY`, not subscription OAuth.
Project `images.enabled` defaults to false, and supported outputs are PNG and JPEG.
Plain version: settle important questions before paying for planning, keep routine feedback quiet, and enable images separately if you want to pay for them.
These integrations are undergoing offline validation; see [current status](docs/status.md) for validation and deployment limits.

## View a saved plan

When presenting a saved validated plan, the coordinator calls `export_plan` with `{"plan_id":"<plan-id>"}` through Claude MCP or the Pi `orchestrator` action and links the returned `html_uri`.
The shared renderer makes a standardized offline HTML page with an accessible SVG graph, standard Mermaid source, and a JSON snapshot; agents do not write page code or need shell access.
Private bundles live under `data/projects/<project>/plan-exports/<plan>/<export>/` and contain `index.html`, `plan.mmd`, and `snapshot.json`.
Export again after material plan, status, or routing changes; the page is static and cannot edit, approve, or dispatch work.
Wave grouping shows dependency depth, not promised simultaneous execution, and worker labels use saved selections rather than guesses from current routing.
Exporting does not publish the plan; warn before public or off-machine sharing of private, untrusted plan or report text.
Plain version: the tool makes a page you can open offline, but it does not update itself or start work.

The CLI provides `bin/orchestrator plan-view PLAN_ID [--open]` and `bin/orchestrator graph PLAN_ID --format mermaid`.
Export does not launch a browser unless `--open` is requested; graph output defaults to JSON as before.
See [plan presentation](docs/workflows.md#present-a-saved-plan) for permissions, metadata, and bundle details.

## Included now

- Four planning roles, with only orchestrator and monitor remaining ongoing roles.
- Validated dependency graphs, bounded critique/revision rounds, and project-scoped conversational approvals.
- Durable jobs, attempt fencing, process identity checks, cancellation, deadlines, and restart reconciliation.
- Per-project notes with revision checks and a single active coordinator.
- Persistent feedback with explicit acknowledgment, monitor failure backoff, and blocking findings.
- Private run artifacts, read-only specialists, resource reservations, and rotating SQLite backups.
- Native Claude hooks/MCP and an owned Pi bridge over the same supervisor.
- Project-configured worker routing, isolated write workspaces, and explicit result acceptance.
- Background workers tracked through Orchestrator tools, without Herder tabs or extra windows.

Claude shows lightweight native Haiku watchers; Pi uses local, no-LLM observers in the installed plugin's real `/agents` menu.
Observers display existing jobs without owning execution; stopping an observer does not cancel its worker.
Pi's installed plugin does not reliably show automatic FleetView cards or unfinished progress text.
See [worker visibility](docs/worker-visibility.md) for attachment, reattachment, testing, and limits.

## Worker setup

Tell the coordinator to configure or repair project routing whenever needed.
After binding, it can repeatedly create, replace, and validate `.orchestrator/crew-dispatch.json` through `setup_project(policy, expected_revision)`, with optional revision checking and no shell-command handoff.
Incomplete drafts and deleted policies can be repaired here; setup is never permanently sealed.
It asks for missing worker preferences instead of inventing models.
Saving configuration does not launch or accept work.
Sole regular routing-policy changes do not make an otherwise clean repository ineligible for isolated snapshots, including untracked files and tracked or staged policy additions, modifications, and deletions.
Real source changes still block snapshot preparation; no stash, commit, ignore rule, or Git exclude write is needed for routing-only changes.
Plain version: give it your worker preferences in chat, and it saves the setup for you.

The CLI remains available if you prefer manual setup:

```bash
./bin/orchestrator routing init --project my-project
# Edit ~/Code/my-project/.orchestrator/crew-dispatch.json
./bin/orchestrator routing validate --project my-project
```

The bound coordinator selects planned and on-demand workers without relabeling plan origins.
For named classifications it chooses `easy`, `hard`, or `very-hard` difficulty, or exact effort; the router supplies the configured model or team.
The monitor still selects supplied legacy pending plan workers.
Without unattended authorization, classification suggestions become `worker.routing_recommended` notifications and the coordinator chooses final difficulty or effort.
With unattended authorization, monitor classification recommendations can select pending planned workers using configured profile effort or `execution.worker_difficulty` (default `hard`), without inventing models or effort.
No separate routing model runs, and an empty policy cannot execute a worker.
Read-only Git audit, research, and design teams use 2 to 8 distinct configured models over the same frozen Git commit and accepted dependencies.
Two rounds compare complete untrusted reports under normal concurrency: four peers mean eight jobs, without guaranteed consensus.
Peers can also exchange live durable messages through `send_team_message` and `read_team_messages`; neither messages nor reports grant new authority.
Child workers use native observers in the existing frontend, never new Herder windows.
Only the parent team's result can be accepted.

Permissions default to `coordinator_approvals=true`, `require_write_approval=false`, and `enforce_monitor_holds=true`; change them locally with `configure_project`.
The coordinator can use `approve_plan`, `resolve_hold`, `approve_worker`, `accept_worker`, `approve_node`, and `cancel_worker` with reasons instead of sending you to an operator CLI.
Plan approval still requires independent review and fresh monitor evidence.
`execution.unattended = true` provides standing project authorization for reviewed-plan approval and candidate acceptance when `remaining_issues` is explicitly empty and live gates pass.
It never supplies required explicit worker approval, including security-audit teams, and only team parents can be accepted.
Write workers use isolated Git worktrees, never your source checkout.
Project ownership, dependency correctness, source isolation, credential limits, and no blind retry of unknown outcomes remain mandatory.
Workers use file tools, plus owned `run_command` for commands and tests when `commands.enabled = true` or `execution.mode = "trusted"`; without it they must disclose checks they could not perform.
Trusted mode always runs worker commands without an OS sandbox; restricted mode runs them in the OS sandbox unless the project explicitly sets `commands.sandbox = false`, which also runs them on the host without an OS sandbox.
`execution.mode = "trusted"` also enables native foreground tools.
`execution.base_ref` selects the Git branch for worker checkouts, not a directory.
Plain version: trusted mode, or turning off the command sandbox, lets tools run commands on your computer, so only enable it for work you trust.
See [project routing](docs/project-routing.md) for the policy schema, team behavior, and conversational permissions.
Plain version: change your preferences and make authorized decisions here, then check results before accepting them, without opening more terminals.

## Deliberately not enabled

Pi visibility uses the already installed `@tintinweb/pi-subagents` plugin when available.
This project does not install third-party plugins automatically.
There is no distributed scheduler, automatic publishing, or guarantee of exactly-once execution.
Standing project authorization does not automatically answer native permission prompts or grant policy-required worker approval.

This is a single-machine implementation, not a Temporal deployment.
The supervisor survives frontend exits, but an OS restart or supervisor crash requires starting it again; binding or resuming a project in either frontend does this.
Native voice, live model access, and Claude's idle wake behavior require interactive acceptance testing.
No per-role dollar cap is imposed on either specialist adapter; deadlines and machine-resource limits still apply.
Plain version: workers must stay within their assigned work; the supervisor does not publish changes or let workers approve themselves.

## Checks and documentation

```bash
machine-resources status
machine-resources run -m 2G -c 2 -d 'Orchestrator tests' -e 5m --hard-limit -- \
  python3 -m unittest discover -s tests -v
```

Tests use real subprocesses with fake model executables and do not spend model credits.
See [frontends](docs/frontends.md), [runtime](docs/runtime.md), [First Mate coverage](docs/firstmate-coverage.md), and [current status](docs/status.md).
