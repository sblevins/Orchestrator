# Native frontends

Claude Code is the preferred frontend; Pi is also supported.
Both remain native interactive terminals and use the same local supervisor and durable project state.
Plain version: either chat program can manage the same jobs without starting a second job manager.
No third-party Pi integration has been selected or installed; plugin selection remains pending.
The repository's Pi extension is a thin owned bridge, not a plugin recommendation.

## Start and bind

Run `bin/orchestrator start --frontend claude` or `bin/orchestrator start --frontend pi` from the Orchestrator checkout.
Use `--home PATH` before `start` to select another private state directory.
Add `--project ID` for a registered project, `--observer` for read-only observation, or explicit `--takeover` to replace its current coordinator.
The launcher provides `ORCHESTRATOR_HOME` and `ORCHESTRATOR_SESSION_ID`.
Bare Pi displays instructions to use the launcher rather than silently creating a different identity.
Claude's `/project`, `/status`, and `/plan` commands use the shared MCP tools.

Model and effort come explicitly from `roles.orchestrator`; a Claude frontend rejects a Codex role rather than silently changing providers.
Pi maps the Claude adapter to `anthropic` and Codex to `openai-codex`.
Claude receives explicit model and effort because the custom settings argument suppresses the local wrapper's model-pin settings.
Pi receives a private per-instance session directory and explicit session ID.
No frontend command enables permission bypass, print mode, bare mode, or terminal key injection.

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
Existing inactive sessions are never reactivated by SessionStart, including sessions displaced by takeover.
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
Both frontends receive the configured orchestrator role and personalization through a private prompt file.
Configured read tools are translated into the native frontend allowlist; native worker and file-edit tools are not included.
Generated Claude MCP settings live at `data/frontend/<session-id>.mcp.json` with owner-only permissions and an absolute CLI path.
The generated server receives the launcher-owned session ID rather than a model-supplied identity.
Notes and worker output are untrusted task data, not permission to change scope or approve work.
The shared API validates project ownership and mutation permissions.

## Offline checks and remaining acceptance

Run `python3 -m unittest discover -s tests -p 'test_frontends.py'` and the equivalent command for `test_hooks.py`.
These tests make no paid calls and cover command construction, hook JSON, takeover safety, prompt delegation, quiet status replies, continuation guards, and disconnect behavior.
Live voice, idle wakeup, preview-channel eligibility, in-session effective model selection, and permission-dialog delivery remain manual integration checks.

Native Claude `/clear` preserves the durable instance while changing the native conversation ID.
Resume uses the recorded native ID without changing the project binding.
Claude may preserve its initial system-prompt snapshot on resume; start a new instance to apply changed role instructions reliably.
For project-specific orchestrator model/preferences, select `--project ID` at launch.
Binding a project later does not hot-swap an already running frontend model.
