# Orchestrator

A neutral, project-scoped coordinator with native **Claude Code and Pi** interfaces.
Claude Code is preferred so its own voice interface remains available.
The fast orchestrator talks with you; a planner develops graph-based plans, an independent critic checks them, and a slow monitor reviews important decisions in the background.

The durable supervisor is a **program**, not a model.
It owns task state, dependencies, process identities, event delivery, notes, and review gates in SQLite.
Plain version: closing a chat does not erase who is working or what still needs attention.

## Start

Requirements: Linux, Python 3.11+, `machine-resources`, authenticated Claude Code and Codex for the default specialist roles, and Pi if using that frontend.
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
Edit any role's model, effort, budgets, deadlines, and resource reservations without changing Python code.
Model identifiers are requests, not a guarantee of account access or measured superiority.

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

## Deliberately not enabled

**Worker routing and worker execution are disabled**, including after plan approval.
The later router must use First Mate's guidelines: the monitor chooses plan workers; the orchestrator chooses unrelated on-demand workers.
Third-party Pi orchestration plugins remain undecided and are not installed by this project.
There is no distributed scheduler, automatic publishing, automatic permission approval, or guarantee of exactly-once execution.

This is a single-machine implementation, not a Temporal deployment.
The supervisor survives frontend exits, but an OS restart or supervisor crash requires starting it again; binding or resuming a project in either frontend does this.
Native voice, live model access, and Claude's idle wake behavior require interactive acceptance testing.
Codex has no enforced dollar cap in this implementation; deadlines still apply.
Plain version: the durable planning foundation is ready to try, but it cannot yet send workers to change your project.

## Checks and documentation

```bash
machine-resources status
machine-resources run -m 2G -c 2 -d 'Orchestrator tests' -e 5m --hard-limit -- \
  python3 -m unittest discover -s tests -v
```

Tests use real subprocesses with fake model executables and do not spend model credits.
See [frontends](docs/frontends.md), [runtime](docs/runtime.md), [First Mate coverage](docs/firstmate-coverage.md), and [current status](docs/status.md).
