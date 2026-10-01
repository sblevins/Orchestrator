# Workers and routing

Workers and routing are enabled in tracked configuration, but **there are no worker model defaults**.
Workers are tracked background jobs, not new tabs or windows.
They currently appear through Orchestrator tools, not native harness sub-agent lists.
See [native visibility research](research/native-worker-visibility.md) before treating those interfaces as interchangeable.
The slow monitor selects work belonging to an approved plan; the foreground orchestrator selects unrelated user-requested work.
An operator may explicitly override a selection through the CLI, but models cannot grant that override or approve their own work.
Plain version: background jobs are available, but you must choose their model policy and authorize changes before they can run.

## Configure project policy

Register and bind the project first, then initialize its policy:

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
A selection supplies the exact model and effort, a rationale, the zero-based rule index or `"default"`, and a candidate index when needed.
The selector chooses the best-fitting rule; deterministic validation does not interpret task prose or choose omitted model axes.
Declared model or effort constraints cannot silently be changed.
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
Commit or ignore the project policy file before requesting write work, so its presence does not make the source checkout dirty.
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
