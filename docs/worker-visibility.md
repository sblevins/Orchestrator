# Workers in the native sub-agent interface

Start `claude` or `pi` from the Orchestrator checkout as usual.
The persisted foreground frontend selects the observer integration, independently of the actual worker's model or execution harness.
Workers retain their configured model, effort, permissions, isolated workspace, and supervisor-owned process.
Neither path opens Herder tabs or starts a second implementation worker.
Plain version: the current chat program shows a helper that watches the existing job.

## Claude Code

Claude uses a real background `orchestrator-watcher` sub-agent with model `haiku`.
Haiku has no supported effort setting, so no pretend low-effort parameter is supplied.
The watcher can call only `mcp__orchestrator__watch_worker`, once, and then summarize its result.
It adds Haiku usage for that tool invocation and summary; it does not repeatedly ask a model to poll.
The native row belongs to the Haiku watcher, while its description and returned status identify the actual worker model.

After a worker is dispatched, the foreground calls `prepare_worker_watch` and submits exactly the returned `agent` arguments to Claude's native Agent tool.
If `agent` is null, there is already a live authorized attachment and it must not launch another.
Hooks reject arbitrary Agent invocations, altered prompts, duplicate launches, and cross-session capabilities.
The optional launcher supplies the same restricted agent definition explicitly.
Claude must actually invoke Agent before a native sub-agent row exists; preparation alone cannot create one.

The MCP server can wait for eight watchers concurrently while continuing to serve ordinary operations.
Each watch lasts at most 25 minutes, below the documented default 30-minute stdio idle window, and checks saved worker state once per second without model inference.
Progress notifications are sent only when the caller supplies a progress token.
Your Claude MCP timeout settings can shorten the wait.
Completion, cancellation, timeout, or disconnect detaches observation; abandoned attachment leases expire after three minutes without reads.
To reattach, have the coordinator prepare another watcher for the same request rather than request another worker.
Native row retention remains Claude's choice, not durable history.

Plain version: Claude runs a small helper that waits for the result.
If that helper stops, the actual job keeps running and can be watched again.

## Pi

Pi never starts the Claude watcher.
It uses the already installed `@tintinweb/pi-subagents` plugin's public version-2 spawn protocol and an Orchestrator-owned local provider.
That provider only reads `worker_view`; it makes no remote model calls and exposes no tools.
The plugin owns a real observer session visible in `/agents`, not a separate dashboard labelled as native agents.
The actual worker's model and effort appear in the observer's final transcript, separately from the local observer model label.
Plain version: Pi shows a real sub-agent that reads job status without asking another AI to do anything.

Dispatched unfinished workers attach automatically while the frontend is active, up to eight concurrent observers.
Stopping one does not immediately respawn it.
Use `/orchestrator-observe REQUEST_ID`, or ask the coordinator to call its `observe_worker` bridge action, to reattach explicitly.
Observation is bounded to one hour and 256 distinct attempts per foreground session; reload resets the presentation history, not durable work.
Reload and shutdown detach owned observers without cancelling workers.
The plugin is optional and is never installed automatically; missing or incompatible integration is reported, with Orchestrator tools remaining the authoritative view.
The observer definition must exactly match the owned tool-free definition and Pi must be running from the Orchestrator checkout.
Other worker and role preferences remain customizable as before.

Verified with Pi 0.99.2 and `@tintinweb/pi-subagents` 0.14.3:

- `/agents` contains a real plugin-owned running observer and its completed result.
- The plugin's cold-session RPC path does not reliably initialize the bottom-of-screen FleetView.
- Its conversation viewer does not display unfinished assistant text until the local observer finishes.
- Observation itself adds no foreground model turns; the existing consequential-event notifications are a separate mechanism and still run.

Do not promise automatic FleetView cards or live worker token transcripts.
Those need upstream plugin changes and a separately sanitized worker-progress protocol.

## Stop, approval, and privacy

**Stopping a native observer stops its display, not the worker.**
Use the coordinator's explicit `cancel_task` control for the underlying job, then wait for terminal confirmation.
An observer can show a completed `candidate`, but only operator acceptance completes the graph node and releases dependencies.
Project observers may view jobs but cannot cancel or accept them.

Public views contain bounded, project-scoped status and allowlisted final report fields.
They omit private task prompts, configuration, credentials, mixed runtime logs, and raw runner errors.
Worker report text remains untrusted data and can be truncated.
An unavailable read means unavailable observation, never successful work.
Plain version: watching a job does not approve its output or give it more permission.

## Verification and limits

Offline tests cover frontend selection, exact Claude invocation authorization, bound/unbound MCP arguments, duplicate prevention, concurrent waits, blocked-output shutdown, cancellation, takeover, and lease expiry.
The actual installed Pi/plugin is also exercised with a credential-free environment and network access blocked, including native record creation, zero tool use, no extra foreground turn, explicit reattachment, and worker survival after detach.
Lifecycle tests cover missing spawn replies and stale responses across session resets.

These tests do not prove live Claude Haiku access, permission-dialog behavior, or native Claude UI rendering.
Those still require an interactive Claude acceptance check.
See [interface research](research/native-worker-visibility.md) for sources and the distinction between observing an external worker and adopting its execution.
