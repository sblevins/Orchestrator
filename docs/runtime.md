# Local runtime

Claude and Pi frontends share one SQLite-backed local supervisor.
The only runnable roles are planner, critic, and monitor; worker dispatch remains disabled.
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

Each attempt uses a private directory below `data/runs/TASK/TOKEN` rather than executing in the project checkout.
The complete prompt is saved in `prompt.txt`, including the tracked role instructions, personalization, canonical source-root context, and task evidence.
Only configured read-only adapter tools are permitted; no permission bypass is added.
Claude receives the canonical project root through `--add-dir`; Codex uses its read-only sandbox and ignores inherited user configuration and rules.
A private working directory and read-only tools are not a hostile-process isolation boundary.

Child environments retain normal authentication but remove `ORCHESTRATOR_SESSION_ID`, `ORCHESTRATOR_FRONTEND`, and `CLAUDECODE` nesting context, and set `ORCHESTRATOR_CHILD=1`.
The runtime never reads credential files.
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
Codex cost is unknown.
Neither adapter has a configured per-role dollar cap; timeouts and machine-resource limits remain enforced.
