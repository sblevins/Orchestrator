# First Mate feature coverage

The reference checkout at `~/Code/firstmate` was fast-forward pulled before implementation.
The audited revision is `8f756bbc287c5bdfacc64a7cc09e8516c64fc919` (`fix(bin): recognize clone roots across path spelling differences (#6306)`).
The [full feature-family audit](research/firstmate-audit.md) records source paths and adopt/defer/reject decisions.
This is a source review of the current feature surface, not a claim to have executed or security-audited every First Mate feature.
No First Mate code was copied into this implementation.

## Adopted patterns implemented here

- One authoritative durable work ledger: SQLite transactions, immutable events, attempt tokens, and terminal-result validation.
- One active coordinator per project: immutable instance binding, observer sessions, and explicit takeover.
- Private project memory: canonical project roots, named notes, compare-and-swap revisions, and separate operating data.
- Saved feedback before notification: per-session inboxes, exact event acknowledgment, and redelivery after reconnect.
- Background supervision: detached runners, bounded monitor calls, resumable Claude monitor conversations, fresh Pi tasks, and cheap polling between calls.
- User-dialog mirroring: complete saved prompts, conservative review classification, and recorded consequential decisions.
- Failure handling: cancellation, deadlines, process-start identity checks, unknown-outcome reporting, monitor cooldowns, and no blind replay.
- Project control: configurable permission gates, blocking findings, project pause, and bound conversational approval and acceptance tools with explicit reasons.
- Narrow host integration: native Claude lifecycle hooks, bounded async wake, optional preview MCP channels, and an owned Pi bridge.
- Resource discipline and diagnostics: shared machine reservations, role configuration snapshots, private bounded logs, doctor output, and rotating SQLite backups.

These are patterns, not claims of drop-in behavioral parity with First Mate.
For example, the current Claude watcher is bounded and does not implement First Mate's complete successor-rearming supervision host.
Plain version: important messages and job records survive interruptions, but not every First Mate automation has been rebuilt.

## New requirements implemented independently

The planning pipeline separates planner, critic, and ongoing monitor responsibilities.
The program validates dependency graphs before critique and calculates readiness from actual dependency states and approval nodes.
Configuration supports local and per-project preferences, replaceable workflow templates, and arbitrary future core-role model identifiers.
The bound coordinator can repeatedly repair routing and patch private project settings, including validated named graph templates, without modifying shared defaults or another project.
Role/model choices are captured for tasks; permissions, concurrency, and worker/routing disable settings remain live.
The monitor cannot hide a blocking finding by acknowledging a notification.
Routine status prompts are saved without automatically paying for a deep review.

Project classifications and legacy FirstMate-compatible rules/default guide selection without automatically populated model defaults.
The coordinator selects planned and on-demand workers while preserving their origins, with classification difficulty or exact effort chosen explicitly.
The monitor still selects supplied pending legacy plan workers.
Classification suggestions become `worker.routing_recommended` notifications unless unattended authorization permits selection using configured profile effort or `execution.worker_difficulty`.
Read-only Git comparison teams run two rounds under ordinary concurrency over the same frozen commit and accepted dependencies, comparing complete untrusted reports without guaranteed consensus.
Live durable send/read tools also let peers exchange messages as untrusted data, never new instructions or permissions.
Workers and team children are tracked background sub-agents with native observers, without new terminal tabs.
Code changes remain in isolated worktrees until separately reviewed and integrated.
Authorized coordinator APIs handle project approvals, while checked result acceptance and dependency enforcement remain mandatory; teams are accepted through their parent only.
See [project routing](project-routing.md) for schemas, configurable permissions, and intrinsic boundaries.
Plain version: configure and direct workers here in chat, then check their results before accepting work that later tasks need.

## Deferred or intentionally excluded

Quota-dependent candidate arrays, floors, and quota-balanced choices fail closed until a supported sanitized quota adapter exists.
Active mid-turn steering, automated test execution, automatic shipping, forge workflows, quota-aware account failover, and worker cost aggregation are not implemented.

Fleet hierarchies, cross-home handoffs, task-axi/Beads adoption, automatic tool updates, social integrations, custom voice services, and third-party Pi graph plugins are not required for the initial local coordinator.
Pi visibility uses the already installed sub-agent observer integration when available; this project does not install third-party plugins for the user.
A visual graph editor, bounded review-loop node, and distributed durability through Temporal are not implemented.
Plan presentation uses the shared `export_plan` renderer for Claude MCP and Pi, with a private static HTML/SVG page, standard Mermaid source, and saved JSON snapshot.
The coordinator links the returned `html_uri` rather than writing Mermaid, HTML, or CSS.
See [workflows](workflows.md#present-a-saved-plan) for export instructions and operator CLI options.
The page reuses the public explainer's luxury/silk palette without becoming a public publishing feature or a live approval and dispatch interface.
Plain version: the program makes a private plan page, not an editor or a way to start work.

The pirate persona, terminal keystroke injection, implicit approval, unrestricted core-specialist shell tools, and silent model fallback are intentionally excluded.
Trusted workers can run commands through owned `run_command`; standing `execution.unattended` authorization permits eligible plan approval and candidate acceptance without granting policy-required worker approval.
Native Claude voice is retained rather than replaced.

## Correction to the earlier research

Current First Mate already supports a Claude primary with a persistent headless Claude supervision conversation.
Its wake queue now includes actor-scoped handling, generation-aware recovery, and unread-outcome recovery.
The earlier checkout's wake-drain criticisms must not be treated as current findings.
See the audit and [Claude compatibility research](research/claude-compatibility.md) for the updated evidence.
