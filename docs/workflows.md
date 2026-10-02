# Planning graphs and workflow templates

Planning defaults to `planning.structure = "graph"` and `planning.workflow = "plan-review"`.
A workflow is a user-editable dependency graph, not a model-routing rule or a fixed list of prompts.
In plain English: each task names the tasks that must finish before it can start.

## Current execution boundary

`orchestrator.graphs` validates plans and computes which nodes may run from a state snapshot.
Templates use exactly the same schema and scheduler as resulting plans, including independent branches and joins.
The planner should receive the selected template as guidance when constructing a task-specific plan, and its output must pass `validate_plan` before being used.
These pure graph APIs do not start processes, persist state, verify acceptance evidence, or authenticate human approvals.
The durable worker service separately enforces these gates and dispatches selected workers.
Nodes may declare mode `read` (default) or `write`; only work nodes may request write mode.
A successful worker remains `awaiting_review` until checked acceptance through bound `accept_worker`, the optional CLI, or standing `execution.unattended` authorization, so its dependents cannot start early.
Unattended acceptance requires an explicit empty `remaining_issues` list and satisfied live gates; it never supplies policy-required worker approval.
Plain version: finishing a worker is not enough; its result must be accepted before the next task starts.
The current planner/critic role interaction is not an arbitrary graph executor.
In plain English: the program checks task order and only starts later work after accepting the required earlier results under the project's rules.

Clarify material unknowns before invoking the paid planner, then obtain independent final review of the saved graph.
`retry_review` retries an eligible failed critic using the saved draft and original review evidence without starting a new planner.
An unknown critic outcome requires investigation rather than automatic replay.
Plain version: settle important questions first, and do not pay to draft the same plan again just because its review failed.

## Select and customize a template

Use `configure_project` to save complete project-local graphs in `planning.templates` and select a name with `planning.workflow`.
These changes write only private `<home>/config/projects/<bound-id>.json`, never another project's configuration or shared defaults.
The active bound coordinator can change templates at any time, without an initial setup seal or operator handoff.
Plain version: ask in chat to change this project's task order without changing anyone else's settings.

The corresponding planning and execution settings are:

```toml
[planning]
structure = "graph"
workflow = "research-review"

[execution]
max_parallel = 3
dependency_failure = "block"
```

Configured `planning.templates` take precedence over file templates with the same name.
For file templates, `load_workflow(home: Path, name: str) -> dict` checks `home/config/workflows/<name>.json`, then tracked `workflows/<name>.json` beside the installed source.
The home workflow directory is shared, administrator-only customization, not a project-scoped edit destination.
A private shared file replaces the entire template, rather than merging individual nodes.
A malformed private file is an error, not permission to fall back silently.
New names can be defined in project-local `planning.templates` without creating shared files.
Project configuration selects a template name, not an arbitrary file path.

Names and node IDs must start with an ASCII letter or digit, followed by ASCII letters, digits, underscores, or hyphens, up to 128 characters total.
Slashes, dots, whitespace, and path traversal are rejected.
Workflow loading rejects symlinks in every directory component and at the file itself, including dangling links, and uses descriptor-relative opens to prevent symlink-swap escapes.
Use a real directory path for `home`, not a symlink alias.
Files must be regular files of at most 4 MiB; duplicate JSON keys are rejected.
In plain English: users choose a short name, and the loader only opens ordinary files in the designated directories.

Included templates:

- `plan-review`: research, then implementation, then independent review, then human approval.
- `research-review`: research, then two independent evidence/risk reviews that may run in parallel, then human approval after both reviews finish.

There are no fixed models or worker-router settings in templates.
Add independent work or review nodes by giving them the same prerequisites; join branches by listing every required branch in `depends_on`.
Retain explicit approval gates wherever human permission is required.
The generic graph validator requires acceptance criteria on every node, but does not require a specific number of reviews or approval nodes.
In plain English: you can change the task order and add checks; the program does not secretly add a human approval step if you remove it.

## Plan schema

`validate_plan(value: dict) -> dict` returns a deep copy, preserving node order and text exactly, with no changes to the input.
All fields below are required, unknown fields are rejected, and there are no implicit dependencies or defaults.

```json
{
  "summary": "Investigate and review a question",
  "assumptions": [],
  "risks": [],
  "questions": [],
  "nodes": [
    {
      "id": "research",
      "title": "Investigate the question",
      "description": "Collect evidence and document conclusions.",
      "depends_on": [],
      "acceptance_criteria": ["Conclusions have traceable evidence."],
      "kind": "work"
    },
    {
      "id": "human-review",
      "title": "Approve the result",
      "description": "Ask the human to review the evidence.",
      "depends_on": ["research"],
      "acceptance_criteria": ["An authorized human explicitly approves."],
      "kind": "approval"
    }
  ]
}
```

Plans contain 1-256 nodes with unique IDs.
Kinds are exactly `work`, `review`, or `approval`.
Titles contain 1-256 characters; summary, description, and list text entries contain 1-8192 characters.
Text must be nonblank and cannot contain NUL characters.
Assumptions, risks, and questions are lists of at most 64 strings, and may be empty.
Acceptance criteria are lists of 1-64 strings.
Dependencies are lists of at most 255 distinct existing node IDs.
Self-dependencies and all other cycles are errors, including disconnected cycles.
Numbers, booleans, tuples, and null are not coerced into strings or lists.

`planning_schema() -> dict` supplies a fresh JSON Schema for structured-output adapters.
The schema describes field types and bounds; callers must still run `validate_plan` for graph-wide checks such as duplicate IDs, missing dependencies, and cycles.
All graph APIs raise `GraphError`, a `ValueError` subclass, for invalid graph inputs or workflow contents.

## Scheduling and state contract

```python
from pathlib import Path
from orchestrator.graphs import load_workflow, ready_nodes, validate_plan

workflow = load_workflow(Path("/real/orchestrator-home"), "research-review")
plan = validate_plan(workflow)
decision = ready_nodes(plan, {"research": "completed"}, max_parallel=2)
assert decision["ready"] == ["evidence-review", "risk-review"]
```

`ready_nodes(plan, states, max_parallel=3, dependency_failure="block")` returns four lists of IDs: `ready`, `blocked`, `cancelled`, and `waiting`.
The result is a scheduling decision, not a replacement state snapshot.
Neither argument is mutated.
Output order is deterministic topological order, breaking ties by the original node position rather than alphabetical order.
In plain English: prerequisites are considered first, and otherwise tasks keep the order the planner chose.

`states` must be a dictionary keyed only by IDs in the plan.
Accepted state strings are `pending`, `ready`, `waiting`, `running`, `completed`, `succeeded`, `failed`, `cancelled`, `blocked`, `approved`, and `unknown`.
Unrecognized strings or non-string states are errors; the explicit `unknown` state waits and never releases a dependency.
An omitted state means `pending`.

- Work and review nodes release dependencies only in `completed` or `succeeded` state, and only when their own prerequisites have succeeded.
- Approval nodes release dependencies only in explicit `approved` state with satisfied prerequisites.
  They never enter `ready`; even `completed`, `succeeded`, or `running` approval nodes stay in `waiting` unless affected by a failure.
- `approved` on a non-approval node stays waiting; it is not a substitute for completed work.
- Only pending, ready, or waiting non-approval nodes with satisfied dependencies can enter `ready`.
  Choosing a node as ready does not complete it, so its children must wait for a later snapshot.
- `max_parallel` is an integer from 1 through 64, excluding booleans.
  Every existing `running` entry consumes a slot, including an inconsistent running approval entry.
  Ready selection uses only the remaining slots; excess runnable nodes remain waiting.
- Existing running work/review nodes and successfully finished nodes are omitted from the four lists.
  This function neither relaunches nor stops running jobs.
- Failed, cancelled, or blocked dependencies affect every descendant, including descendants with inconsistent running or successful states.
  With `dependency_failure="block"`, affected descendants enter `blocked`; with `"cancel"`, they enter `cancelled`.
  Existing blocked and cancelled nodes retain their original classification, while failed nodes are omitted but still propagate failure.
  Independent branches remain eligible.

Cancellation and blocking lists are recommendations to the caller, not confirmation that an operating-system process has stopped.
The caller must handle actual termination, persist decisions, and provide trustworthy completion and approval states.
Acceptance criteria describe required evidence, but the scheduler cannot prove that the evidence exists.
In plain English: a task failure stops dependent tasks from becoming ready, and human approval must be recorded explicitly by trusted code.
