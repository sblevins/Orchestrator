# Native worker visibility

Investigated 2026-10-01 against installed Claude Code 2.1.287, Pi 0.99.2, and the installed `@tintinweb/pi-subagents` 0.14.3 package.
This is a supported-interface investigation, not proof from a paid end-to-end Claude session.
No hosted inference, user-session changes, package installation, or private harness-state modification was performed.

## What the current implementation does

Orchestrator runs durable workers independently of the foreground harness.
Its `workers`, `worker`, and `task` operations expose those jobs through supervisor tools.
That does not register workers in Claude's native Agent view or the Pi add-on's `/agents` fleet.
The prior description of workers as tracked background sub-agents was not sufficiently clear about this distinction.
Plain version: the coordinator knows about the workers, but the harness's own agent list does not.

## Claude Code

No documented external-agent registration or transcript-adoption API was found.
A `.claude/agents/*.md` definition configures a Claude-managed agent, not a custom executor or an existing external process.
The SDK controls the query it creates; it does not expose attachment to an unrelated foreground terminal's sub-agent registry.
Hooks observe existing agents and can add context, but do not create native agent entries.

A supported composition is a real background Claude watcher sub-agent with narrowly restricted supervisor MCP tools.
The watcher receives an existing worker identity, waits for its result, and reports it without starting another implementation worker.
The actual worker can retain its configured Claude Code or Pi provider, model, and effort.
However, the native row and transcript belong to the Claude watcher; the row's model is the watcher model, not the external worker model.
It adds Claude model usage for tool invocation and reporting, with possible further calls after timeouts or reconnection.
Current official documentation explicitly allows MCP tools in background sub-agents.
Plain version: Claude can show a Claude helper that watches the chosen worker, but the helper is an extra agent, not the worker itself.

A long-running MCP call from the main conversation can instead appear as an **MCP background task** in `/tasks`.
Claude documents automatic backgrounding after two minutes, configurable through `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`.
Calls inside sub-agents do not automatically become separate MCP background tasks.
Progress notifications can update the task row, but it remains a task rather than a native sub-agent.
This avoids adding a separate watcher-model conversation, though the foreground still invokes and handles the tool.
A checklist entry, channel message, status line, or immediately returned worker ID does not create a native running-agent entry.

### Cancellation and recovery

Closing or stopping a watcher is not automatically cancellation of an independently supervised worker.
Standard MCP cancellation is optional and race-prone; SubagentStop is not a documented reliable callback for every user stop or process death.
The safe initial contract is to detach the watcher when its connection disappears and keep durable worker cancellation explicit.
Do not label a native stop control as stopping the real worker until its propagation has been tested in the actual harness.
Resuming the foreground must reattach to the same durable worker identity, never launch a replacement implementation worker.
Native task-row retention is also shorter-lived than Orchestrator's durable history.
Plain version: stopping the display must not secretly lose, repeat, or cancel the underlying work.

## Pi

Pi's public extension API supports tools, commands, renderers, widgets, custom screens, session entries, and an extension event bus.
It does not provide a built-in external-worker registry.
The familiar Agent/FleetView and `/agents` interface in this installation comes from `@tintinweb/pi-subagents`, not Pi core.
That package's public version-2 cross-extension RPC supports `ping`, `spawn`, and `stop`, but not attaching or updating external workers.
Calling `spawn` creates new package-owned execution rather than adopting a supervisor worker.
Emitting its lifecycle notification names does not insert an agent into its manager.
Do not mutate the add-on's private manager or deep-import its private UI to simulate attachment.

An Orchestrator-owned view can use `registerCommand`, `ctx.ui.setWidget`, `ctx.ui.custom`, `appendEntry`, and `registerEntryRenderer`.
It would show the actual durable workers inside Pi without new terminals, new model sessions, or installing another plugin.
It must be clearly named as an Orchestrator worker view, not advertised as integration into the existing `/agents` fleet.
A supported external-worker adapter in that add-on would be a separate upstream feature.
Plain version: we can show the workers inside Pi, but cannot currently insert them into that existing add-on's list through its public interface.

### Display and authority requirements

Join each worker request to its task because a running worker's request remains `queued` while execution state lives on the task.
Display exact selected model and effort, unknown before selection, and distinguish `candidate` from accepted completion.
Use bounded, project-scoped summary/detail reads instead of repeatedly transferring every full worker record.
Sanitize terminal controls in all model-provided content.
A failed refresh means stale or unavailable information, never successful completion.
Observers may inspect workers but not cancel them; a current replacement coordinator may control workers created by an older session.
Viewing or acknowledging a result must not approve writes or accept graph completion.

Progress display must not use model-triggering chat messages for every update.
The existing consequential-event wake behavior is separate from UI polling and must not be silently changed.
Do not expose the runtime's mixed private stdout/stderr log as a live transcript.
A safe progress endpoint would need bounded allowlisted events, attempt-bound cursors, and explicit reset/truncation markers.
Claude's current JSON result format is not a token-streaming interface, so lifecycle and final-result display must remain honest about that limitation.

## Verified without paid model calls

The existing native-start, native-Pi, and worker-API test groups passed: 21 tests.
Pi RPC probes confirmed widget requests and display-only custom entries with no streaming, pending messages, or model conversation messages.
Actual Pi pseudoterminal probes rendered widgets and custom cards in both regular and fullscreen TUI modes and exited cleanly.
Those probes verify public UI primitives, not a finished worker dashboard.
Claude evidence consists of installed version/help and fresh official documentation; native watcher UI, stop propagation, and live model usage remain untested.

## Decision required before implementation

The exact request cannot be satisfied by direct external-worker adoption through the current public interfaces.
Choose between explicitly labelled Claude watcher sub-agents, accepting their additional model usage and distinct cancellation semantics, or Claude background-task visibility without a separate watcher model.
Pi can independently use an owned in-harness worker view; integrating the existing `/agents` fleet requires an additional supported adapter contract.
Do not silently substitute a status list or a proxy agent while claiming that the external worker itself became a native sub-agent.

## Primary references

- [Claude Agent tool](https://code.claude.com/docs/en/tools-reference#agent-tool-behavior)
- [Claude sub-agent configuration](https://code.claude.com/docs/en/sub-agents#supported-frontmatter-fields)
- [Claude foreground and background sub-agents](https://code.claude.com/docs/en/sub-agents#run-subagents-in-foreground-or-background)
- [Claude MCP automatic backgrounding](https://code.claude.com/docs/en/mcp#automatic-backgrounding-of-long-tool-calls)
- [Claude hook events](https://code.claude.com/docs/en/hooks#subagentstart)
- [Claude SDK Query API](https://code.claude.com/docs/en/agent-sdk/typescript#query)
- [MCP cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)
- [MCP Tasks do not prescribe UI](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)
- Installed Pi package documentation: `docs/extensions.md`, `docs/tui.md`, `docs/rpc-extension-ui.md`, `docs/session-format.md`, and `examples/extensions/subagent/README.md`.
- Installed `@tintinweb/pi-subagents` package: README and `src/cross-extension-rpc.ts` public protocol.
