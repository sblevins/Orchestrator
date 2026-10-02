# Native frontends

Claude Code is the preferred frontend; Pi is also supported.
Both remain native interactive terminals and use the same local supervisor and durable project state.
Plain version: either chat program can manage the same jobs without starting a second job manager.
Pi worker visibility uses the installed `@tintinweb/pi-subagents` plugin through public APIs only; no plugin is installed automatically.
The owned extension remains the supervisor bridge, and native observers do not become worker executors.
See [worker visibility](worker-visibility.md) for Claude-only Haiku watchers and Pi's local observers.

## Start and bind

Run `claude` or `pi` directly from `~/Agents/Orchestrator`.
Claude discovers `CLAUDE.md`, project lifecycle hooks, and `.mcp.json`; Pi discovers `AGENTS.md` and `.pi/extensions/orchestrator.ts`.
Native trust prompts still require user approval.
The coordinator asks for a project, binds the instance, loads notes/status and project setup state, and starts the shared supervisor as needed.
Both frontends expose `project_setup` and repeatable `setup_project` for routing drafts, updates, and repairs, plus `project_settings` and `configure_project` for project settings.
The active bound coordinator can configure its project at any time, with no initial setup seal or operator handoff.
Routing writes touch only the bound project's `.orchestrator/crew-dispatch.json`; settings writes touch only private `<home>/config/projects/<bound-id>.json`.
Scoped writes never modify another project or shared defaults.
These narrowly scoped writes do not require Bash and do not themselves grant source changes or execution approval.
The coordinator asks for missing worker preferences instead of requiring operator commands or choosing models without your direction.
Plain version: open the normal chat program here; the project supplies its coordinator setup automatically.

Claude startup resolves its own ancestor process identity and native conversation ID into a durable instance.
Concurrent terminals never share an active-project setting.
`/clear` changes the conversation ID but preserves the same process's coordinator binding.
An in-process native `/resume` selects the target conversation's own instance and releases the previous conversation for another window.
A rejected second process cannot use prompts, tools, or exit hooks to alter the original instance.
Native resume can reopen a cleanly closed instance, but never silently reclaim an instance displaced by takeover.
Pi uses its native session ID and a process ownership record, with orderly shutdown and reload handling.
Existing native frontend processes are registered with `machine-resources`; background jobs still start through reservations.

Native Pi selects the configured foreground model/effort at startup and preserves in-session model changes across turns and reloads.
A missing model or authentication blocks requests visibly rather than silently falling back.
Native Claude's foreground model is controlled by native settings and CLI pins, not SessionStart output.
The repository defaults to Sonnet/low, but this machine's existing wrapper pins Opus/high at higher precedence.
Use `/model` and `/effort` to change that session; no global wrapper or user model setting is modified here.
Planner, critic, and monitor settings remain controlled by Orchestrator in both frontends.

The optional `bin/orchestrator start --frontend claude|pi` launcher remains supported.
Use `--home PATH` before `start` for a separate private state home, and `--project ID`, `--observer`, or explicit `--takeover` as needed.
It supplies explicit foreground model/effort flags and a stable identity, so Claude's global model pin does not override that launch.
Claude's `/project`, `/status`, and `/plan` commands use the shared MCP tools.
No path enables permission bypass or terminal keystroke injection.

## Voice and feedback

Claude keeps ownership of its terminal, `/voice`, microphone access, and permission dialogs.
Native voice requires a supported local terminal, microphone, and claude.ai account; SSH and direct API-key authentication do not provide this voice contract.
Voice and live model delivery still require manual acceptance testing on the operator's machine.

Claude lifecycle hooks record prompts through the shared API, observe meaningful external tool actions, and return bounded project context.
Prompt persistence includes unbound sessions and oversized input; the API owns truncation, storage, and the `needs_review` policy.
Routine status prompts remain recorded without requesting a monitor review.
Read-only tools and Orchestrator tools do not create duplicate tool-observation events.
A Stop reply is recorded only after an outstanding substantive user prompt or external tool observation, not after every status response.
Stop continuation is guarded by `stop_hook_active`, and SessionEnd closes only the frontend session without cancelling jobs.
Only explicit native resume reconnects a cleanly closed Claude session; an ordinary startup does not revive an inactive session, and takeover-displaced sessions cannot reconnect implicitly.
Child hooks are disabled by `ORCHESTRATOR_CHILD=1`.

The default Stop watcher uses documented `asyncRewake`, waits at most 27,000 seconds, and has a 28,800-second hook timeout.
The CLI owns the single-per-session lock and exits 2 with pending attention to wake idle Claude.
This is a bounded window started by a lifecycle hook, not an unlimited always-on push service.
Plain version: after an answer, Claude can receive job feedback for a limited time without anyone typing.
After that window, use status or another turn to check pending events and start another window.

`start --frontend claude --channels` explicitly opts into research-preview MCP channels and the development-channel confirmation prompt.
Ordinary MCP registration does not authorize channels; authentication and organization policy still apply.
The default does not request preview channels.
Pi polls updates through CLI calls using private payload files, sends each new event ID as a follow-up turn, and stops polling when the session shuts down or reloads.
Reload reconstructs delivered event IDs from the active conversation branch to avoid repeated model turns.
Delivery is not acknowledgment: both frontends retain pending events until explicit acknowledgment after handling findings.
Plain version: seeing a message does not mark the problem as solved.

## Private data and authority

The launcher generates owner-only hook settings with absolute installed paths, so a separate private home does not break hooks.
It loads user settings plus its generated settings, excluding project/local settings to avoid registering the repository hooks twice.
Launched frontends receive the configured role and personalization through a private prompt file; native startup supplies them through Claude hook context or Pi prompt sections.
In guarded mode, Claude PreToolUse and Pi tool-call guards block implementation tools and unrelated plugin tools, even when the native interface exposes them.
`execution.mode = "trusted"` permits native foreground tools; it is not an OS sandbox or permission to exceed the user's project request.
Repeatable project routing and settings APIs are explicit owned-tool exceptions to foreground read-only behavior, not a blanket shell bypass.
Project-scoped decision APIs also support authorized approvals, hold resolution, and explicit worker acceptance; the operator CLI remains optional.
The ordinary local Claude MCP configuration uses an explicit session ID from startup context, and the hook rejects attempts to use another instance's ID.
Configured read tools are translated into the native frontend allowlist; file-edit tools and untracked worker dispatch remain blocked.
In guarded mode, Claude Agent is permitted only for an exact prepared watcher invocation; Pi spawns observers through its owned public-RPC integration.
The monitor waits for foreground completion and `monitoring.quiet_seconds` (20 by default); only blocking monitor findings interject, while info and warning findings remain silent.
Generated Claude MCP settings live at `data/frontend/<session-id>.mcp.json` with owner-only permissions and an absolute CLI path.
The generated server receives the launcher-owned session ID rather than a model-supplied identity.
Notes and worker output are untrusted task data, not permission to change scope or approve work.
The shared API validates project ownership and mutation permissions.

## Offline checks and remaining acceptance

Run `python3 -m unittest discover -s tests -p 'test_frontends.py'` and the equivalent command for `test_hooks.py`.
These tests make no paid calls and cover command construction, hook JSON, takeover safety, prompt delegation, quiet status replies, continuation guards, and disconnect behavior.
The suite also tests installed Pi RPC auto-discovery without launcher variables or an explicit extension flag, with no provider calls.
Native-style Claude parent processes test clear/resume aliases, and the installed Claude CLI confirms project MCP discovery.
Live voice, idle wakeup, preview-channel eligibility, and permission-dialog delivery remain manual integration checks.

Native Claude `/clear` preserves the durable instance while changing the native conversation ID.
Resume uses the recorded native ID without changing the project binding.
Claude may preserve its initial system-prompt snapshot on resume; start a new instance to apply changed role instructions reliably.
For project-specific orchestrator model/preferences, select `--project ID` at launch.
Binding a project later does not hot-swap an already running frontend model.

## External validation workers

A process marked `NO_MISTAKES_GATE`, like a supervisor child marked `ORCHESTRATOR_CHILD=1`, is not an interactive coordinator.
Claude lifecycle hooks and long-running Stop watches return immediately, and the Pi extension does not change its native tools, model, or lifecycle.
The marker selects integration behavior only: it creates no Orchestrator session, project binding, approval, or worker grant.
Explicitly launching `orchestrator start` starts a normal coordinator instead.
Plain version: a code-review or test agent can work on this repository without being forced to set up a project or wait for coordinator notifications.
