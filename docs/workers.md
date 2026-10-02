# Workers and routing

Workers and routing are enabled in tracked configuration, but **there are no worker model defaults**.
Workers are tracked background jobs, not new tabs or windows.
Durable records remain in Orchestrator tools; frontend-specific observers also represent jobs in native sub-agent interfaces.
Claude uses Haiku watchers and Pi uses local no-LLM plugin observers.
Stopping an observer does not cancel the worker, and Pi FleetView/live partial text remain limited.
See [worker visibility](worker-visibility.md) for exact behavior and reattachment.
The slow monitor selects work belonging to an approved plan; the foreground orchestrator selects unrelated user-requested work.
An operator may explicitly override a selection through the CLI, but models cannot grant that override or approve their own work.
Plain version: background jobs are available, but you must choose their model policy and authorize changes before they can run.

## Configure project policy

Register and bind the project, then ask the coordinator to complete initial setup.
The bind response includes setup state; no shell command or separate operator terminal is needed for this stage.
The coordinator uses `project_setup` to inspect the phase, policy revision, and validation, and `setup_project` to create or fill the policy.
It asks for missing worker models, effort levels, and routing preferences, and saves the choices you approve rather than inventing profiles.
Plain version: the coordinator can finish its own setup instead of handing the work back to you.

Calling `setup_project` without a policy creates only the empty `{"rules": []}` placeholder, idempotently.
Calling it with `policy` and the current `expected_revision` validates and saves the initial configuration at the bound project's fixed policy path.
Changes visible at either revision check require rereading rather than overwriting them.
Setup calls serialize through the shared database; manual editors do not share that lock.
The revision check is not atomic filesystem compare-and-swap against arbitrary external writers, so do not manually edit the policy while conversational setup is saving it.
Plain version: use chat setup or manual editing, not both at the same time.
A nonempty initial policy must contain at least one currently executable profile with explicit model and effort; invalid or entirely blocked choices are rejected before saving.
Validation does not test live model access or approve execution.

This setup write is limited to the active coordinator and the policy file; observers, displaced sessions, shell commands, source edits, and arbitrary paths do not receive this permission.
An existing configured policy, a successful setup save, or a dispatched worker closes initial setup permanently for that project.
Removing the file afterward does not reopen it.
Later policy changes retain the operator workflow and existing policy-refresh/approval gates.
The program never commits the policy, edits `.gitignore`, or changes Git's exclude settings for you.

Manual setup remains optional:

```bash
bin/orchestrator routing init --project PROJECT_ID
bin/orchestrator routing show --project PROJECT_ID
bin/orchestrator routing validate --project PROJECT_ID
```

`routing init` creates `.orchestrator/crew-dispatch.json` inside the registered project and refuses to overwrite an existing file.
It writes `{"rules": []}`, an intentionally unroutable placeholder, not a set of recommended models.
`show` and `validate` can inspect that empty policy; add your chosen FirstMate-style rules before requesting execution.
The fallback location is `config/crew-dispatch.json` under the Orchestrator home, used only when the project policy is absent.
An unreadable, malformed, oversized, or empty project policy blocks routing instead of falling back to another policy.
Missing policy also blocks selection, including operator overrides, until a valid policy is configured.

Policy contains `rules` and/or an optional `default` profile.
Each rule has natural-language `when` text and a `use` profile or candidate array; optional fields include `why`, `approval: "captain"`, `min_confidence`, `floor`, and `select: "quota-balanced"`.
A profile names a `harness` and may constrain `provider`, `model`, and `effort`.
A selection supplies an explicit model ID or supported family name and effort, a rationale, the zero-based rule index or `"default"`, and a candidate index when needed.
The selector chooses the best-fitting rule; deterministic validation does not interpret task prose or choose omitted model axes.
Declared model or effort constraints cannot silently be changed.
For Claude profiles, `"model": "Opus"` or `"model": "Fable"` follows the native family alias; `Sonnet` and `Haiku` also work.
Family casing is ignored when comparing a choice to policy, but a family and an exact version are not interchangeable without an operator override.
The saved profile fixes the selector; a family selector can resolve to a newer version on a later launch.
Exact IDs remain unchanged, and policy content/digests are not rewritten by normalization.
See [model families](configuration.md#model-families-or-exact-versions) for provider and catalog limits.
Maximum effort requires an explicit policy preference or operator override and must still be supported by the executor.
Plain version: the model explains which configured rule fits, and the program checks that its choice follows that rule.

Claude Code is the executor for Anthropic models only.
Pi requires an explicit non-Anthropic provider and uses the owned SDK bridge pinned to 0.99.2.
Direct Codex CLI execution is unsupported; `openai-codex` is a Pi provider, not a separate worker harness.
Unsupported executors, effort levels, or unavailable exact Pi models stop execution without silent fallback.
Pi tasks use fresh ephemeral sessions with no resume, canonical auth storage, and no inherited plugins.
See [adapters](adapters.md) for authentication, file-tool controls, and completion validation.

**Quota-dependent arrays and floors currently fail closed because no supported sanitized quota evidence adapter is available.**
This includes even a one-element candidate array, profile or rule floors, and `select: "quota-balanced"`.
Caller-provided quota numbers cannot unblock dispatch.
An operator can explicitly override the policy match or configure a non-quota-dependent profile, but overrides still require approval and valid executor settings.
`routing validate` distinguishes valid policy syntax from currently routable profiles; it does not prove live model access or authorize a launch.
Plain version: when a rule requires quota information the program cannot verify, it refuses to choose a worker.

## Selection and launch gates

Worker requests retain a persisted user-message origin, policy digest, quota uncertainty, generation, concrete selection, and task link.
Plan requests additionally retain the immutable plan version and node identity.
The foreground cannot replace a plan node's brief or select its worker as unrelated work.
Monitor selections must come from its saved, successful, accepted result and match the supplied request context.

Launch checks current policy, selection authority, project pause and holds, current approved plan, accepted prerequisites, and concurrency limits.
A policy change requires an explicit request refresh before launch; refreshing clears selection and approval.
An attempted request cannot be refreshed, reselected, or blindly replayed.
Unknown outcomes remain visible for operator inspection.
Plain version: changing the plan or policy does not quietly reuse an old permission.

Operator plan approval authorizes the plan's declared write nodes, subject to all remaining gates.
Unrelated write requests need explicit worker approval.
Rules marked `approval: "captain"` and operator overrides require separate worker approval even for approved plan work.
Approval-only graph nodes use their own operator command and require accepted prerequisites.

```bash
bin/orchestrator approve PLAN_ID
bin/orchestrator approve-worker REQUEST_ID --reason "Reviewed the selected scope and write authority"
bin/orchestrator approve-node PLAN_ID NODE_ID --reason "Verified the approval condition"
```

An explicit override is a concrete profile with `harness`, `model`, `effort`, `rationale`, and a Pi `provider` when applicable:

```bash
bin/orchestrator override-worker REQUEST_ID --choice-file /path/to/private-choice.json
```

The choice file must be a private regular file owned by the operator, with no group or other permissions.
Override selection does not replace the required `approve-worker` command.
Approval and acceptance commands are operator-only, not exposed as model tools.

## Workspaces, results, and acceptance

Read workers inspect the project with read tools only.
Read workers with accepted dependency commits receive a prepared isolated checkout containing those changes, not the unchanged source checkout.
Write workers require a clean Git repository root and receive a separate worktree and branch created by the supervisor.
There is no automatic stash or source-branch modification.
The sole cleanliness exception is an untracked, regular `.orchestrator/crew-dispatch.json` setup file.
You do not need to commit or ignore that file just to start a worker.
Tracked policy changes, other untracked files, source changes, and unsafe policy paths still block preparation.
Accepted prerequisite commits can be merged into the isolated worker checkout to supply dependency changes; this never integrates a candidate into the source branch.
Unsupported Git filters, merge drivers, unsafe paths, or provenance changes block preparation or capture.

Worker tools allow reading and permitted file edits only, with no shell commands or tests.
The supervisor, not the model, prepares Git worktrees and captures bounded diffs and signed local candidate commits.
Git signing and signature verification must already be configured for the repository; the supervisor never changes your identity or disables signing.
These controls are not an operating-system sandbox against hostile programs running as the same user.
No worker automatically merges into the source branch, pushes, or opens a tab or window.
Plain version: a worker prepares files separately, and the supervisor saves those changes for review without changing your current checkout.

A successful worker report contains exactly `summary`, `changes`, `checks`, and `remaining_issues`.
The last three fields are arrays of text, and the report must not claim tests were run by a worker that had no test tool.
Successful execution creates a `candidate`; a plan node becomes `awaiting_review`, not `completed`.
Inspect the saved report, diff, commit, and remaining issues, and run any required verification separately before accepting:

```bash
bin/orchestrator accept-worker REQUEST_ID --reason "Reviewed the diff and independently verified the acceptance criteria"
```

Acceptance rechecks current gates and marks the node completed, allowing dependent work to proceed.
Acknowledging an inbox message is not result acceptance.
Acceptance does not merge or push; shipping remains a separate explicit operator action.
Plain version: a finished job waits for your review, and later jobs cannot rely on it until you accept it.
