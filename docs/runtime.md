# Local runtime

Claude and Pi frontends share one SQLite-backed local supervisor.
Planner, critic, monitor, and authorized temporary workers run as supervised background processes.
Workers never create Herder tabs or interactive windows.
In plain English: either frontend can leave while the same background work continues.

## Entry points

The Python API exports `ensure_supervisor(home)`, `service_status(home)`, `supervise(home, once=False)`, `run_task(home, task_id, token)`, and Linux `process_identity(pid)`.
Use `ensure_supervisor` from a frontend rather than launching a daemon directly.
It checks `machine-resources status`, reserves 256 MiB and one CPU for the daemon, and waits up to 15 seconds for startup.
Missing registry executables and rejected reservations produce actionable errors rather than bypassing resource accounting.

For diagnostics, `python3 -m orchestrator.runtime supervise --home /absolute/home --once` performs one scheduler pass.
A manually started long-running supervisor must itself be launched through `machine-resources run -m 256M -c 1 -e 86400s -d 'orchestrator supervisor' --hard-limit -- python3 -m orchestrator.runtime supervise --home /absolute/home`.
The internal runner entry point is `python3 -m orchestrator.runtime run-task --home /absolute/home --task-id ID --token TOKEN`; production dispatch wraps it in the shared supervisor task resource reservation, with an estimate of the shared task timeout plus 60 seconds.
Do not launch that entry point directly for production work.

## Ownership and recovery

A filesystem `flock` makes the supervisor unique per home, with a separate startup lock for concurrent frontend requests.
Task claims receive unique attempt tokens; registration atomically accepts only the current starting attempt.
Each runner is detached from the supervisor and creates its harness in a separate process group without a shell.
The runner sends heartbeats, checks cancellation, and enforces a monotonic deadline independently of frontend or supervisor lifetime.
Linux boot identity and process start ticks distinguish a live owner from a reused PID.
In plain English: restarting a frontend or scheduler does not start a second copy of an existing task.

The supervisor watches resource launcher exits while alive.
Following supervisor loss, a starting attempt expires after its saved configuration's `stale_seconds`, even when the registry rejected it before any runner registered.
Expiration invalidates the attempt through its terminal state, so a delayed launcher cannot later start work with that token.
There is no automatic retry of failed reservations or unknown work.
An operator can inspect `machine-resources status` and explicitly request new work.

Live runner identities are retained even if their heartbeat is temporarily stale.
Known-dead runners become `unknown` after the stale interval, and their recorded harness process groups are killed when their identity still matches.
A harness exceeding its persisted deadline plus the stale interval is also terminated and marked unknown during recovery.
A timeout or interrupted process is never reported as success.
There is a small nontransactional interval between spawning a harness and recording its PID; abrupt machine/process failure in that interval cannot establish definitive ownership of an orphan.
In plain English: uncertain work is reported as uncertain, not silently repeated or called finished.

## Inputs, results, and privacy

Each attempt stores artifacts in a private directory below `data/runs/TASK/TOKEN`.
Write workers execute in isolated Git worktrees; read workers have controlled reading tools.
The complete prompt is saved in `prompt.txt`, including the tracked role instructions, personalization, canonical source-root context, and task evidence.
Core specialists have read-only tools; authorized write workers add file editing tools.
Trusted workers, and restricted workers with `commands.enabled = true`, additionally have owned `run_command` for command and test execution.
Trusted mode always runs worker commands without an OS sandbox; restricted mode runs them in the OS sandbox unless the project explicitly sets `commands.sandbox = false`, which also runs them on the host without an OS sandbox.
No permission bypass is added.
Claude receives the canonical project root through `--add-dir`; Pi uses owned path-limited file tools without inherited orchestration integrations.
A private working directory and read-only tools are not a hostile-process isolation boundary.

Child environments retain normal authentication but remove `ORCHESTRATOR_SESSION_ID`, `ORCHESTRATOR_FRONTEND`, and `CLAUDECODE` nesting context, and set `ORCHESTRATOR_CHILD=1`.
Credential reuse is adapter-owned and must not expose secrets in prompts or logs.
Harness stdin reads the private prompt file, avoiding command-line argument limits and prompt exposure in process listings.
The combined stdout/stderr log is private and capped at 8 MiB; excess output fails the attempt and terminates its process group.
Adapter result files are checked for oversized output and symlinks, but only the adapter's verified stdout terminal protocol establishes success.
In plain English: a partial answer or a leftover output file is not enough to mark the job done.

## Review and monitoring

Planner output must be strict JSON and is installed through `Store.install_graph` before a critic is attached.
The critic receives the original request, validated graph, and current evidence.
Its JSON verdict and findings pass through `Store.apply_critique`.
Accepting a changes-requested critique atomically schedules its replacement planner and acknowledges the old result; the store enforces `planning.max_review_rounds`.
A scope change invalidates all live old plans, including drafting and reviewing plans.
Planner failures do not automatically retry, and neither a critique nor a revision executes workers.

Monitor scheduling coalesces persisted review-worthy events and includes notes, project state, and the exact `reviewed_through` cursor in a persisted snapshot prompt.
It waits for foreground completion and `monitoring.quiet_seconds` (20 by default); only blocking monitor findings interject, while info and warning findings remain silent.
Routine messages marked `review_required=false` do not independently wake a monitor but remain available in the next eligible snapshot.
Monitor-generated events do not recursively trigger monitoring.
The store rejects responses naming any other cursor and applies exponential backoff to failures.
A successful, processed monitor conversation resumes only with exactly matching saved configuration, rotating after every 20 successful turns.
Every resumed turn still receives fresh evidence.
In plain English: the monitor remembers its conversation, but must inspect the new saved facts and identify exactly which facts it checked.

The supervisor records heartbeat metadata and makes an online SQLite backup at most once per day; the store retains three backups.
Tests use real subprocesses and fake local harness/registry executables, never paid model calls.

The stored Claude cost is the harness-reported value, not a verified per-turn delta across resumes.
Do not sum resumed reports as if that accounting has been validated.
Pi cost remains unknown unless its adapter supplies validated accounting evidence.
Neither adapter has a configured per-role dollar cap; timeouts and machine-resource limits remain enforced.

## Worker lifecycle

Policy snapshots pin the chosen provider, model, effort, and routing rationale before dispatch.
The bound coordinator selects both planned and on-demand workers.
Accepted monitor responses may select supplied pending legacy plan requests.
Classification suggestions are recommendations unless unattended authorization permits selection using configured profile effort or `execution.worker_difficulty`.
Current project permissions, required approvals, policy freshness, scope version, holds, pauses, dependencies, and leases are rechecked before execution.
Project permissions remain live controls rather than frozen task settings.
The worker service validates structured reports separately from the harness terminal protocol.
Program-captured worktree/diff/commit evidence stays separate from the worker's claims.
A successful worker is a candidate, not an automatically successful graph node.
Checked acceptance uses the bound `accept_worker` API, optional CLI, or standing `execution.unattended` authorization with an explicit empty `remaining_issues` list and satisfied live gates.
Unattended progression never creates explicit worker approval required by policy, including security-audit teams.
Only accepted prerequisites release dependent work.
Plain version: save who was chosen and why, check permission again before starting, and wait for review before using the result.
