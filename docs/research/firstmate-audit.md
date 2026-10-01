# Current Firstmate audit for Orchestrator

## Baseline and scope

- Repository: `/home/sb-bravo-labs/Code/firstmate`.
- HEAD and locally recorded `origin/main`: `8f756bbc287c5bdfacc64a7cc09e8516c64fc919`.
- Commit date: `2026-10-01T07:24:35-07:00`.
- Commit subject: `fix(bin): recognize clone roots across path spelling differences (#6306)`.
- Working tree was clean when inspected.
- Read-only source review; no Firstmate commands, installation, tests, agents, or network calls were run.
- Reviewed README, VISION, complete architecture, configuration feature headings and relevant sections, complete script catalog, public/internal skill inventories, Claude hooks, supervision-host documentation, and selected implementation bodies.
- Implementation verification is narrow, not a security audit of every script; documented behavior and source inspection are not fresh runtime validation.
- Paths below are relative to the Firstmate repository unless explicitly absolute.

## Most important correction to the old analysis

Current Firstmate already supports a **Claude Code primary with a persistent headless Claude supervision conversation**.
The supervision host is now default-on for Claude, with an explicit opt-out, and supports attended as well as away operation.
It is not necessary to run Pi to obtain the persistent-background-supervisor design.
Its dispatcher currently imports shared logic from `.pi/extensions/lib/fm-branch-dispatch.ts`, so copying the implementation unchanged would retain Pi-directory coupling even though its engine is Claude.

The old wake-drain critique must not be carried forward unchanged.
The current drain has actor-scoped consumption, generation-bound recovery acknowledgement, unread-status recovery, an incremental durable decision fold, bounded presentation locks, unusable-row retirement, lost-outcome backstops, and repeated presentation of unprocessed captain outcomes.
The new host also re-arms a successor before returning attended main-only wakes.

Plain English: Firstmate can now keep the main Claude conversation quiet while another Claude conversation handles routine work, and it records unfinished messages so they can be shown again after a failure.
It is a much stronger reference than the earlier checkout, but still not a ready-made implementation of the requested four-planner, neutral Orchestrator.

## Adoption matrix

This matrix records design decisions, not a claim that every adopted feature is implemented.
See `../firstmate-coverage.md` for the shipped subset and explicit deferrals.

**Adopt** means adopt the contract or pattern in the new repository, not import Firstmate wholesale.
**Defer** means preserve an extension point without building it in the initial planning/supervision release.
**Reject** means do not inherit that implementation or default, not that the feature is inherently bad.

| Feature family | Current Firstmate evidence | Decision for `~/Agents/Orchestrator` |
| --- | --- | --- |
| One user interface; scripts for mechanics, agents for judgment | `VISION.md`, `README.md` | **Adopt.** Claude Code is the preferred frontend and Pi is also supported; the persistent orchestrator owns decisions and the monitor produces evidence, not a competing mandate. |
| Four planning roles before ongoing operation | Ship/scout/secondmate briefs exist; no equivalent four-role planning protocol in audited feature surface | **Adopt as new design.** Define four bounded planning contracts and their durable handoff, then stop those roles; do not turn them into four permanent supervisors. |
| Small root instruction contract, conditional procedures | `AGENTS.md` routing model; VISION states 9,000-word ceiling; `.agents/skills/` | **Adopt principle, reject size/default copy.** Write a much smaller neutral `CLAUDE.md` and role documents; load operational procedures when needed. |
| Separate shared code and private operating home | `FM_HOME`, `config/`, `data/`, `state/`, `docs/configuration.md` | **Adopt.** Keep reusable Orchestrator code separate from private projects, notes, credentials, and runtime records. |
| Durable task contracts | `fm-brief.sh`, `fm-dod-lib.sh`, explicit mode/kind checks | **Adopt.** Record intent, non-goals, deliverable, evidence, authority, owner, dependencies, and generation before work begins. |
| Ship vs scout task shapes and scout promotion | `fm-promote.sh`, `scout-completion`, `ship-landing` skills | **Defer execution mechanics.** Preserve research versus mutation distinction in planning now; router and shipping stay out of initial scope. |
| Backlog lifecycle coupled to task lifecycle | `fm-backlog-transition-lib.sh`, `fm-spawn.sh`, `fm-tasks-axi.sh` | **Adopt atomic-transition principle.** Choose one authoritative work ledger; do not require tasks-axi or Beads merely because Firstmate uses them. |
| Cross-home queued handoffs | `fm-backlog-handoff.sh`, `fm-backlog-receive.sh` | **Defer.** Not needed for two persistent local roles. |
| Single active session ownership | `fm-lock.sh`, `fm-session-lock-lib.sh` | **Adopt.** Read-only second frontend must not acquire mutation authority accidentally. |
| Per-task mutation leases | `fm-lease-lib.sh`, `fm-lease.sh` | **Adopt with stronger generation identity.** Hold the same lock across check and mutation; monitor should usually need no mutation lease. |
| Durable wake queue | `fm-wake-lib.sh`, `fm-wake-drain.sh` | **Adopt.** At-least-once notification plus idempotent processing; publishing a notification is not handling it. |
| Actor-scoped grants and acknowledgements | `fm-wake-grant.sh`, drain claim files | **Adopt if both roles consume events.** Never let a monitor acknowledge an orchestrator-owned decision. |
| Recovery episode generation separate from queue sequence | `fm-wake-drain.sh --ack-through ... --recovery-generation ...` | **Adopt.** An old acknowledgement may close old rows but cannot clear a newer failure episode. |
| Recovery from unread/hidden decisions | `fm-classify-lib.sh`, drain OPEN DECISIONS and UNREAD STATUS | **Adopt.** Later progress must not overwrite unresolved decisions or answers. |
| Lost-outcome and contradiction checks | drain STATUS OUTCOME BACKSTOP and RECORD DIVERGENCE | **Adopt.** Compare task, decision, and outcome records, surface disagreement, and never infer user approval from a resolution-looking sentence. |
| Durable outcome store with read vs processed cursors | `fm-branch-outcome.sh`, `fm-branch-report.sh` | **Adopt.** Seen, reported, and acted upon are separate states. |
| Event-driven token-free watcher | `fm-watch.sh`, `fm-watch-arm.sh` | **Adopt.** Cheap deterministic polling/events between slow model reviews; no model turn just to report unchanged state. |
| Persistent background Claude review | `fm-supervision-host.sh`, `fm-supervision-engine-lib.sh` | **Adopt architecture, narrow authority.** Persistent slow monitor should review bounded snapshots and escalate; Firstmate's branch can steer/recover, which is broader than a read-mostly monitor. |
| Always-on slow holistic review | Firstmate has heartbeat/current-state review and inactive reconciliation; `/stow` explicitly is not durable repo/PR reconciliation | **Adopt as new explicit responsibility.** Monitor overdue obligations, missing evidence, contradictions, stuck delivery and stale plans, not just panes. |
| Successor watcher before handoff | `fm-supervision-host.sh` attended path; `fm-watch-arm.sh` | **Adopt.** Handling an event must not leave the system unwatched. |
| Claude startup/Stop/PreTool hooks | `.claude/settings.json`, `fm-sessionstart-run.sh`, `fm-claude-stop-autoarm.sh`, `fm-turnend-guard.sh` | **Adopt narrow verified hooks.** Re-test installed Claude version before relying on `asyncRewake`, hook timing, or print-mode flags. |
| User-dialog mirror into background reviewer | `fm-host-mirror.sh`, Claude UserPromptSubmit/Stop registrations | **Adopt bounded context feed.** Treat mirrored conversation as contextual data, not fresh authority; explicit decisions remain in durable records. |
| Engine failure circuit breaker | Host health latch: two engine errors, five-minute initial cooldown, exponential bound to one hour | **Adopt.** Background failure returns ownership to orchestrator; do not silently disable monitoring. |
| Background process identity and cleanup | Host predecessor records, engine descendant snapshots, `fm-timeout-lib.sh` | **Adopt identity-bound cleanup, improve containment.** Same-UID detached descendants can escape sampled reaping; use resource-managed process groups/cgroups where available. |
| Stale and busy detection | `fm-busy-lib.sh`, `fm-busy-event.sh`, `fm-crew-state.sh` | **Adopt semantic states.** Unknown is neither busy nor idle, and elapsed time is inspection evidence rather than permission to kill. |
| Stale-worker incarnation fencing | `fm-busy-event.sh` generation on arm/apply/progress/retire | **Adopt and extend.** Require generation on all result/decision writes, not just lifecycle records. |
| Wake-consumer progress separate from process liveness | `fm-watch.sh` secondmate wake-stall scan | **Adopt.** A living monitor/orchestrator may still fail to consume events; track oldest actionable item progress. |
| Proven-dead restart and bounded recovery attempts | `fm-secondmate-liveness-lib.sh`, startup and watcher drivers | **Adopt policy for the two persistent roles.** Unknown/unreachable must not spawn duplicate owners; persist restart count and escalation. |
| Pause, declared external wait, user-held decision | `fm-captain-hold.sh`, `fm-classify-lib.sh`, `captain-hold-lifecycle` | **Adopt explicit workflow states.** Persist reason, decision owner, clearing condition and next review; retain pending work. |
| Process pause/resume | `fm-control.sh` allows interrupt, exit, relaunch; `docs/agent-control.md` explicitly says resume is not a verb | **Adopt checkpoint-and-relaunch, not claimed generic resume.** User-facing pause must distinguish stopping new work, waiting for a condition, interrupting a turn, and ending a process. |
| Away and quiet postures | `fm-afk-contract.sh`, `fm-afk-launch.sh`, `fm-afk-return.sh`, `/afk`, `/quiet` | **Adopt posture records, defer authority relocation.** Quiet is attended; away does not imply consent. Start with identical monitor authority in both. |
| Return catch-up gate | `fm-afk-return.sh`, durable outcome drain | **Adopt.** Present unresolved decisions before returning to normal dispatch. |
| Scoped authority and approval gates | `fm-lease-forbid_branch`, `fm-gate-refuse-lib.sh`, `ask-user-authority` skill | **Adopt stricter explicit gates.** Approval binds action, scope and current version; a good report is never authorization. |
| Natural-language away mandate as authority interpretation | AFK v2 deliberately stores verbatim words without parsing; guarded scripts retain mechanical gates | **Reject as sole machine-enforced permission source.** Preserve user words, but approve concrete capabilities separately; retain conservative judgment for ambiguity. |
| Instruction and runtime version awareness | Session-start AGENTS hash/re-emission; hook/extension versions; tool probes; guarded update/restart | **Adopt and expand.** Record repository commit, policy digest, schema version, Claude version, role generation and loaded version per persistent process. Do not equate a pulled checkout with a reloaded process. |
| Model cost telemetry | Engine JSON result parses usage and cost; resumed total converted to per-turn delta; host log | **Adopt.** Distinguish reported cost estimate from billed cost; track cache, input/output, elapsed time and missing telemetry. |
| Quota alerts | `fm-procevent-quota.sh`, `fm-quota-axi-lib.sh` | **Adopt optional observability.** Unknown quota must stay unknown; do not silently change model/account. |
| Spend budget | AFK `spend_max_concurrent_workers`; engine turn bounds/rotation | **Adopt concurrency cap; no per-role monetary budget.** This AFK field is a worker-count cap, not a dollar/token ceiling. No fleet-wide hard monetary budget was established by this review. |
| Quota-aware worker router | `fm-quota-choose.sh`, `quota-array-dispatch`, crew-dispatch profiles | **Defer explicitly.** Do not smuggle router work into monitor model selection. |
| External typed dispatch resolver | `fm-dispatch-resolve.sh`, typesafe.ai/Jev, optional never-send list | **Defer; reject initial dependency.** It sends brief text externally and is unrelated to initial two-role reliability. |
| Notes and durable commitments | Task bodies, captain preferences, learnings, inbox replies, public-followup obligations | **Adopt.** Task notes belong to tasks; personal preferences remain private; promised actions need explicit open/closed records. |
| Context reset and compaction recovery | Session-start context re-emit, `/stow`, durable briefs; host rotates after default 20 turns | **Adopt.** Persist handoff before reset, reconstruct from records, and never make correctness depend on a model summary. |
| Memory budget and archival | `fm-startup-memory-budget.sh`; stow pinned/aging/perishable tiers and cold archive | **Adopt simplified version.** Startup memory is bounded; archive provenance rather than delete; pinned safety/authority never disappears automatically. |
| Compact-adviser disabling on unattended launches | `COMPACT_ADVISER_DISABLE=1` in launch contract | **Defer tool-specific switch.** Adopt the requirement that unattended compaction cannot wait indefinitely for interactive advice. |
| Private per-home configuration and brief additions | `config/brief-include.md`, captain/shared preference ownership | **Adopt.** No Stephen-specific identity, projects or account names in the reusable template. |
| Durable steering inbox plus constant doorbell | `fm-task-inbox-lib.sh`, `fm-send.sh`, bounded re-ring | **Adopt for role-to-role messages.** Persist payload before notification; duplicate notification should not repeat the action. |
| User capture inbox and receipts | `fm-inbox.sh note/announce/reply/receipts/ready` | **Adopt minimal note/receipt interface.** Permit capture while Claude is busy without pretending the note was processed. |
| Message versus process-control planes | `fm-send.sh` versus `fm-control.sh` | **Adopt.** A lifecycle command must never be sent as an instruction the model might reinterpret. |
| Correlated replies and missing-reply recovery | `fm-pending-reply-lib.sh`, `fm-secondmate-report.sh` | **Adopt for orchestrator/monitor requests.** Require request/result correlation, bounded retries and durable escalation. |
| Operational inputs distinguishable from user commands | `fm-operational-input.sh`, provenance-guarded status append | **Adopt.** System notifications and external content are data, never user authority. |
| Trusted process-event sources/custom checks | `fm-procevent.sh`, `fm-check-register.sh`, `fm-check-lib.sh` | **Adopt small internal registry.** Bind approved executable/config bytes and capture result before announcing; defer third-party package ecosystem. |
| Deterministic condition-to-action automation | `fm-procevent-when.sh`, one-fire marker and trust hash | **Defer actions.** Initial monitor may check and notify, not run a new unattended automation system. |
| Pluggable external process-event adapters | `fm-extension.mjs`, `docs/extension-bindings.md` | **Defer.** Security/package complexity not needed for initial lifecycle. |
| Health snapshots and dashboards | `fm-fleet-snapshot.sh`, `fm-fleet-view.sh`, `fm-bearings-snapshot.sh`, home-summary JSON | **Adopt structured read-only health first.** Show queue age, last ack, owner generation, monitor freshness, held decisions, budget and coverage gaps. |
| Interactive visual fleet board and review feedback | `fm-bearings-board.sh`, `fm-procevent-lavish.sh` | **Defer.** Preserve snapshot API; add UI only if requested. |
| Notification-wedge active alarms | `fm-supervise-daemon.sh`, `docs/wedge-alarm.md` | **Adopt explicit delivery-health alarm.** Do not promise phone reach; configure approved notification routes separately. |
| Tool update checks including PATH shadowing | `fm-tool-update-check.sh`, `config/watched-tools.json` | **Adopt read-only slow check.** Installed-but-not-effective is distinct from update available; never auto-install. |
| Guarded self-update and persist-gated restart | `fm-update.sh`, `fm-secondmate-restart.sh`, `fm-ff-lib.sh` | **Adopt version reporting now; defer automated fleet updater.** Update neutral code through normal review, then explicit handoff/restart. |
| Worktree isolation and landed-only cleanup | `fm-spawn.sh`, `fm-teardown.sh`, slot ownership checks | **Adopt requirement now, defer worker implementation.** Never delete unlanded work; pooled-slot ownership needs generation proof. |
| Multiple terminal backends | tmux, Herdr, experimental Zellij/Orca/cmux; Codex App not selectable | **Defer all but one chosen local transport.** Keep transport separate from Claude frontend and authority. |
| Persistent local/remote secondmates | Provisioning, SSH job worker, remote caches and doctor | **Defer.** Two persistent local roles do not require a hierarchy or distributed fleet. Never fall back remote work to local implicitly. |
| Delivery modes, forges, PR merge/CI review | no-mistakes/direct-PR/local-only; GitHub/GitLab/Gerrit boundaries | **Defer execution.** Adopt future exact-head evidence and approved merge contract; do not build forge automation before worker router. |
| Published contribution follow-up and background PR signals | `fm-contributions.sh`, exact-head judgments and measured actor coverage | **Defer integration, adopt evidence-age principle.** Background supervision is not an independent code-review engine; validation remains external. |
| Fresh project clones and safe branch pruning | `fm-fleet-sync.sh` | **Defer automatic pruning.** Explicit fetch/FF readiness checks can join worker launch later. |
| Resource isolation | Launch env allowlist, bounded subprocesses, isolated labs, `fm-jev-mem-guard.py` diagnostics | **Adopt requirements, not sufficiency claim.** Integrate this machine's `machine-resources` before heavy work; add enforced process bounds where needed. Diagnostics are not reservations or a sandbox. |
| Provider account selection | `fm-worker-account-lib.sh`, per-home Claude account pin with auth preflight and credential unsets | **Adopt per-process selection.** Pin persistent Claude roles too, separately from future workers; never mutate a global login. |
| GitHub account selection and signed identity | No `GH_TOKEN`, `auth switch`, or `machine-resources` matches found in scoped `bin/` search | **Add explicit local adapter.** Firstmate worker-provider pins do not establish Stephen's repo-owner-based gh/SSH/signing rules. |
| Commit attribution | Default-off Claude/Devin attribution and pane-scoped git hook chaining | **Adopt no-attribution policy.** Avoid importing intrusive hooks until their interaction with existing repository hooks is tested. |
| Public Relay, social posting and promised-final replies | X/Discord, `fm-public-followup*`, typed obligation/result IDs | **Defer integrations; adopt durable obligation pattern.** Public posting is not required for local Orchestrator. |
| Mail and spoken interface | `fm-mail*`, `fm-voice*`, inbox STT/ask | **Defer.** Avoid extra credentials, external text/audio disclosure and additional frontend channels. |
| Calm transcript rendering | Pi extension; Claude early-access function-hook mod | **Reject initial dependency.** Presentation hiding is not correctness, and experimental frontend modifications conflict with a simple Claude frontend. |
| Pi/other harness-specific adapters | `.pi/`, `.omp/`, `.opencode/`, `.cursor/`, `.grok/` | **Reject initial inclusion.** Reuse vendor-neutral contracts only; do not install Pi or inherit its session assumptions. |
| Testing and failure-injection discipline | queue/host/account/control/lab regression suites and opt-in live E2E | **Adopt.** Test real Claude hook delivery separately from stubbed state-machine tests; failure recovery is part of MVP, not polish. |

## Narrow verification of critical background mechanisms

### 1. Wake drain: recent fixes are present in executable code

`bin/fm-wake-drain.sh` was read in full.
Its main path claims rows not held by a live branch grant, while branch acknowledgement deletes only sequence IDs present in its grant.
Main acknowledgement reclaims only unreserved rows at or below its cutoff, not newly arrived higher rows.
Queue rows are retained through presentation and removed by a later explicit acknowledgement.
Recovery marker retirement separately compares the printed generation, so acknowledging old work cannot silently dismiss new downtime.

The following are implemented, not merely aspirational documentation:

- `retire_unconsumable_rows_locked`: removes truncated/non-sequenced rows which no actor could ever acknowledge, with bounded diagnostics.
- `reclaim_stale_branch_grant_locked`: releases dead grants so work becomes consumable again.
- `print_branch_held_notice`: explains a nonempty queue entirely held by the branch.
- Presentation lock waits are bounded by `FM_STATUS_PRESENTATION_LOCK_TIMEOUT`, default 10 seconds.
- `print_status_sections`: prepares output before committing presentation cursors.
- OPEN DECISIONS folds durable logs rather than trusting the most recent line.
- UNREAD STATUS retains informational notes and reserved-key reply resolutions not represented by that fold.
- STATUS OUTCOME BACKSTOP compares status endpoints/identities against bounded outcome indexes.
- BRANCH OUTCOMES repeats unprocessed captain outcomes, collapses by task under byte limits, asks for current-state checks, and prints a separate `mark-processed` acknowledgement.
- Branch ack now retires inactive-outcome/check receipts too, preventing away-mode repeated escalations.
- Dead drain scratch files and task-owned watcher markers have cleanup paths.

Relevant history observed with `git log -- bin/fm-wake-drain.sh`:

| Commit | Fix |
| --- | --- |
| `f42a629` | Bound presentation lock waits, #3475 |
| `4eb587d` | Prevent routine updates hiding actionable status, #3268 |
| `85d6c72` | Safely split handling by actor, #2953 |
| `d977128` | Resurface statuses missed by wake handling, #3495 |
| `88fb3c0` | Self-heal outcome indexes on first drain, #3509 |
| `f4d7875` | Prevent stale supervision wake loops, #3672 |
| `3af74fe` | Every counted row presentable or retired, #3950 |
| `c33b3f6` | Retire check-row receipts on branch ack, #5731 |
| `9b52cf5` | Attended Claude/Cursor supervision, #5748 |
| `ade7133` | Silence routine no-change outcomes, #5808 |
| `3c2a91d` | Check current state before re-presented cutover outcomes, #5925 |
| `6b0f5a0` | Retire task watcher markers and orphan journals, #5997 |
| `2d833ff` | Treat quiet records as attended, #6064 |

Residuals worth carrying into new design: presentation lock timeout can return a skipped diagnostic without presenting status, and status-presentation failures are deliberately tolerated on the queue path.
Queue mutation locks remain blocking.
Output reaching stdout is not proof that a person or model understood it; outcomes therefore require processing acknowledgement.
These are reasons for explicit health and retry contracts, not grounds to repeat the obsolete claim that the drain simply loses or endlessly hides wakes.

Plain English: messages stay saved until handled, each reader can remove only its own messages, and old acknowledgements cannot clear a newer failure.
If a read is blocked, the system needs to say so and try again rather than pretend there was nothing to read.

### 2. Claude background host: real separate engine with bounded acceptance

`.claude/settings.json` registers SessionStart, PreToolUse guards, UserPromptSubmit mirror, Stop guard, Stop mirror, and `fm-claude-stop-autoarm.sh` with `asyncRewake: true` and 28,800-second timeout.
`fm-supervision-engine-lib.sh` actually builds Claude `-p` arguments with `--safe-mode`, `--system-prompt-file`, `--tools Bash,Read`, `--permission-mode dontAsk`, `--allowedTools Bash Read`, configured model and session/resume selection.
It deliberately does not use bare mode because the verified configuration needs OAuth.
The host's default park is 27,000 seconds, below that hook timeout; its default turn bound is 1,200 seconds and conversation rotation is 20 turns.

The host implementation around lines 901-964 injects branch actor and lease holder, waits for the bounded engine, releases leases, checks granted rows still queued, parses the complete JSON result, and accepts a handled wake only if result success, a report receipt, and no unacknowledged granted rows all hold.
Engine failure invalidates its resumed conversation and returns the wake to main.
The documentation records real Claude measurements at 2.1.278 and 2.1.281; this review did not establish the version installed on Stephen's machine or rerun those proofs.

Plain English: the background Claude must finish successfully, save what it found, and finish its messages before the main conversation treats the job as handled.
A quiet or crashed process alone is not success.

### 3. Leases and stale-worker protection: useful, not hostile-agent isolation

`fm-lease-lib.sh` serializes guard and mutation on a lease-command lock and recognizes only `main` and `branch`.
Lease liveness requires both a living recorded PID and equality with the current session-lock holder.
Its own header explicitly labels the threat model confused-agent-grade, notes PID-reuse residuals, and says main's tasks-axi path lacks an executable backlog lease guard in this scope.
It is not a security boundary against a process deliberately rewriting environment or same-user files.

`fm-busy-event.sh` mints a generation on arm and validates exact generations for apply/progress/retire, protecting a new incarnation from late old hooks.
The watcher contains independent secondmate queue-stall tracking and bounded inbox delivery recovery; a live endpoint is not accepted as proof that its queue drains.

Plain English: these checks stop accidental double work and old workers reporting as new ones, but a program with access to all the same files can bypass them.
The new monitor should be given less ability to change files, not just told not to.

### 4. Signal delivery: inbox before doorbell

`fm-task-inbox-lib.sh` records messages atomically with per-task sequences and `handled/` acknowledgements.
The watcher retries a short notification with default 90-second grace and three attempts, preserves pending human composer text, and surfaces dead/missing endpoints without typing into them.
`fm-inbox.sh` separately supports user request IDs, saved-but-unannounced outcomes, repairable announce, durable reply and bounded receipts.
Do not confuse either receipt with completion of a task; Orchestrator should distinguish delivery, acceptance, processing and resulting action.

Plain English: save the full message first, then tell the other role to read it.
If the notification fails, the message is still there.

### 5. Budgets, accounts, resources and versions need extra work

- Cost: engine telemetry is implemented, but concurrent-worker limits are not monetary budgets.
- Accounts: Claude worker pinning is implemented with preflight and higher-precedence credential unsets; GitHub identity routing is a separate unmet integration requirement here.
- Resources: `fm-jev-mem-guard.py` reads memory/swap/RSS and reports status; it is not the shared reservation system required on this machine.
- Isolation: an environment allowlist and `--safe-mode` stop accidental inheritance of some configuration, not same-user file access or arbitrary Bash execution.
- Versioning: `fm-session-start.sh` hashes startup AGENTS and can re-emit changed instructions, while updates use guarded restart/nudge paths; this is not a demonstrated complete per-worker loaded-code/policy/schema manifest.
- Memory: stow is a session-knowledge sweep with archival, not an autonomous long-term reconciliation daemon, and its rules explicitly admit that gap.

Plain English: Firstmate has useful readings and warnings, but Orchestrator still needs its own machine reservation, correct account selection, and record of what each live role actually loaded.

## Critical source map

| Concern | Start here |
| --- | --- |
| Claude frontend wiring | `.claude/settings.json`, `docs/supervision-protocols/claude.md`, `docs/supervision-protocols/supervision-host.md` |
| Host engine and lifecycle | `bin/fm-supervision-host.sh`, `bin/fm-supervision-engine-lib.sh`, `docs/supervision-host.md` |
| Watcher continuity | `bin/fm-claude-stop-autoarm.sh`, `bin/fm-watch-arm.sh`, `bin/fm-watch.sh`, `docs/watcher-continuity.md` |
| Queue/recovery/ack | `bin/fm-wake-lib.sh`, `bin/fm-wake-drain.sh`, `bin/fm-wake-grant.sh` |
| Shared branch eligibility | `bin/fm-branch-dispatch.mjs`, `.pi/extensions/lib/fm-branch-dispatch.ts` |
| Outcome records | `bin/fm-branch-outcome.sh`, `bin/fm-branch-report.sh`, `bin/fm-branch-prompt.sh` |
| Leases/approval | `bin/fm-lease-lib.sh`, `bin/fm-gate-refuse-lib.sh`, `bin/fm-captain-hold.sh`, `bin/fm-afk-contract.sh` |
| Status semantics and stale incarnations | `bin/fm-classify-lib.sh`, `bin/fm-busy-lib.sh`, `bin/fm-busy-event.sh`, `bin/fm-crew-state.sh` |
| Inbox/correlation/control | `bin/fm-task-inbox-lib.sh`, `bin/fm-pending-reply-lib.sh`, `bin/fm-inbox.sh`, `bin/fm-send.sh`, `bin/fm-control.sh` |
| Restart and version changes | `bin/fm-session-start.sh`, `bin/fm-startup-network.sh`, `bin/fm-update.sh`, `bin/fm-secondmate-restart.sh` |
| Health and snapshots | `bin/fm-supervision-lib.sh`, `bin/fm-guard.sh`, `bin/fm-fleet-snapshot.sh`, `bin/fm-home-summary-refresh.sh` |
| Cost/quota/accounts | `bin/fm-supervision-engine-lib.sh`, `bin/fm-procevent-quota.sh`, `bin/fm-worker-account-lib.sh` |
| Memory | `.agents/skills/stow/SKILL.md`, `skills/stow/SKILL.md`, `bin/fm-startup-memory-budget.sh` |
| Dependency updates | `bin/fm-tool-update-check.sh`, `docs/configuration.md` watched-tools section |
| Regression starting points | `tests/fm-wake-queue.test.sh`, `tests/fm-supervision-host.test.sh`, `tests/fm-claude-stop-autoarm.test.sh`, `tests/fm-host-mirror.test.sh`, `tests/fm-control-relaunch.test.sh` |
| Live evidence, not rerun here | `docs/verification/supervision.md`, `tests/fm-supervision-host-live-e2e.test.sh`, `tests/fm-supervision-host-attended-live-e2e.test.sh` |

## Recommended boundary for the new repository

Build a neutral, Claude-first repository rather than a renamed Firstmate clone.
Four planning roles should produce an approved durable operating plan; afterwards retain only the persistent orchestrator, a narrowly authorized slow monitor, and deterministic supporting processes.
Adopt the reliable event, receipt, generation, authority and recovery contracts now, with fault-injection tests and actual Claude hook verification.
Defer worker routing, fleet hierarchies, forge automation, and custom social/voice interfaces; support both native Claude Code and Pi frontends; make shared resource reservations and per-command account selection mandatory from the first heavy or authenticated operation.
