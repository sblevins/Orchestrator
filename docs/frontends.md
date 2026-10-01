# Native frontends

Claude Code is the preferred frontend; Pi is also supported.
Both remain native interactive terminals and use the same local supervisor and durable project state.
Plain version: either chat program can manage the same jobs without starting a second job manager.
No third-party Pi integration has been selected or installed; plugin selection remains pending.
The repository's Pi extension is a thin owned bridge, not a plugin recommendation.

## Start and bind

Run `claude` or `pi` directly from `~/Agents/Orchestrator`.
Claude discovers `CLAUDE.md`, project lifecycle hooks, and `.mcp.json`; Pi discovers `AGENTS.md` and `.pi/extensions/orchestrator.ts`.
Native trust prompts still require user approval.
The coordinator asks for a project, binds the instance, loads notes/status, and starts the shared supervisor as needed.
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
Claude PreToolUse and Pi tool-call guards block implementation tools and unrelated plugin tools, even when the native interface exposes them.
The ordinary local Claude MCP configuration uses an explicit session ID from startup context, and the hook rejects attempts to use another instance's ID.
Configured read tools are translated into the native frontend allowlist; native worker and file-edit tools are not included.
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
