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
- Human control: blocking findings, explicit approval commands, project pause, and no model-facing approval tool.
- Narrow host integration: native Claude lifecycle hooks, bounded async wake, optional preview MCP channels, and an owned Pi bridge.
- Resource discipline and diagnostics: shared machine reservations, role configuration snapshots, private bounded logs, doctor output, and rotating SQLite backups.

These are patterns, not claims of drop-in behavioral parity with First Mate.
For example, the current Claude watcher is bounded and does not implement First Mate's complete successor-rearming supervision host.
Plain version: important messages and job records survive interruptions, but not every First Mate automation has been rebuilt.

## New requirements implemented independently

The planning pipeline separates planner, critic, and ongoing monitor responsibilities.
The program validates dependency graphs before critique and calculates readiness from actual dependency states and approval nodes.
Configuration supports local and per-project preferences, replaceable workflow templates, and arbitrary future core-role model identifiers.
The monitor cannot hide a blocking finding by acknowledging a notification.
Routine status prompts are saved without automatically paying for a deep review.

FirstMate-compatible project profiles now guide worker selection, with no automatically populated model defaults.
The monitor selects plan workers; the orchestrator selects unrelated workers, both using best-fit natural-language rules rather than ordered keyword matches.
Workers are tracked background sub-agents, without terminal tabs, and code changes remain in isolated worktrees until separately reviewed and integrated.
Program-enforced approval and acceptance gates prevent models from releasing their own dependencies.
Plain version: workers follow your policy and keep their changes separate until you accept them.

## Deferred or intentionally excluded

Quota-dependent candidate arrays, floors, and quota-balanced choices fail closed until a supported sanitized quota adapter exists.
Active mid-turn steering, automated test execution, automatic shipping, forge workflows, quota-aware account failover, and worker cost aggregation are not implemented.

Fleet hierarchies, cross-home handoffs, task-axi/Beads adoption, automatic tool updates, social integrations, custom voice services, and third-party Pi graph plugins are not required for the initial local coordinator.
No plugin choice has been made for the user.
A visual graph editor and distributed durability through Temporal are also not implemented.
The current program exposes a validated graph and readiness through tools and the CLI.

The pirate persona, terminal keystroke injection, implicit approval, unrestricted specialist shell tools, and silent model fallback are intentionally excluded.
Native Claude voice is retained rather than replaced.

## Correction to the earlier research

Current First Mate already supports a Claude primary with a persistent headless Claude supervision conversation.
Its wake queue now includes actor-scoped handling, generation-aware recovery, and unread-outcome recovery.
The earlier checkout's wake-drain criticisms must not be treated as current findings.
See the audit and [Claude compatibility research](research/claude-compatibility.md) for the updated evidence.
