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
Private `config/local.toml` overrides them; `config/projects/PROJECT_ID.toml` adds project preferences.
If you have no private configuration yet, run `./bin/orchestrator config init`.
Edit each role's model and effort without changing Python code.
Use `model = "Opus"` or `model = "Fable"` to follow that supported family, or an exact ID such as `claude-opus-5-5` to keep a version pin.
`Sonnet` and `Haiku` also work; family names are case-insensitive and existing pins stay unchanged.
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

Workers use a project-local FirstMate-compatible routing policy, with no default worker model or effort.
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

## Included now

- Four planning roles, with only orchestrator and monitor remaining ongoing roles.
- Validated dependency graphs, bounded critique/revision rounds, and explicit operator approval.
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

Tell the coordinator to finish initial project setup.
After binding, it can create and validate `.orchestrator/crew-dispatch.json` through its own setup tools, without asking you to run shell commands.
It asks for missing worker model/effort preferences instead of inventing defaults.
Initial setup permissions close after configuration; they never grant source-code writes or worker approval.
An untracked regular worker-policy file does not make an otherwise clean repository ineligible for write workers.
Plain version: give it your worker preferences in chat, and it saves the setup for you.

The CLI remains available if you prefer manual setup:

```bash
./bin/orchestrator routing init --project my-project
# Edit ~/Code/my-project/.orchestrator/crew-dispatch.json
./bin/orchestrator routing validate --project my-project
```

The slow monitor chooses plan-associated workers; the orchestrator chooses unrelated on-demand workers.
Both follow the same project policy, with explicit provider/model/effort selections saved before launch.
No separate routing model runs.
An empty policy is intentionally not enough to execute a worker.
Write workers require operator approval and work in isolated Git worktrees, never your source checkout.
Successful results need operator acceptance before graph dependents start.
Workers use file tools only: they cannot run shell commands or tests, and must disclose checks they could not perform.
See [worker routing](docs/workers.md) for policy format, permissions, and operator commands.
Plain version: choose your worker preferences once, then review permissions and results here without opening more terminals.

## Deliberately not enabled

Pi visibility uses the already installed `@tintinweb/pi-subagents` plugin when available.
This project does not install third-party plugins automatically.
There is no distributed scheduler, automatic publishing, automatic permission approval, or guarantee of exactly-once execution.

This is a single-machine implementation, not a Temporal deployment.
The supervisor survives frontend exits, but an OS restart or supervisor crash requires starting it again; binding or resuming a project in either frontend does this.
Native voice, live model access, and Claude's idle wake behavior require interactive acceptance testing.
No per-role dollar cap is imposed on either specialist adapter; deadlines and machine-resource limits still apply.
Plain version: workers can inspect or edit within their granted scope, but cannot publish changes or approve themselves.

## Checks and documentation

```bash
machine-resources status
machine-resources run -m 2G -c 2 -d 'Orchestrator tests' -e 5m --hard-limit -- \
  python3 -m unittest discover -s tests -v
```

Tests use real subprocesses with fake model executables and do not spend model credits.
See [frontends](docs/frontends.md), [runtime](docs/runtime.md), [First Mate coverage](docs/firstmate-coverage.md), and [current status](docs/status.md).
