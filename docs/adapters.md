# Specialist subprocess adapters

`orchestrator.adapters.build_command(config, role, prompt, cwd, output_path, session_id=None, *, project_root=None, stdin_prompt=False)` returns an argv list, without launching a process or writing a file.
It validates configuration through `role_config` and raises `AdapterError` on invalid configuration or arguments.
Runtime appends the role instructions to the prompt before calling it.
`session_id` means resume that harness conversation, never select the latest conversation or invent a supervisor ID.
Fresh invocations let the harness allocate its ID.
Plain version: this module prepares a command and checks its answer; it does not run or save the job.

## Verified command surface

The baseline is Claude Code 2.1.286 and codex-cli 0.146.1.
Local help and an intentionally invalid Codex invocation confirmed positional argument separation with `--` without making a model call.
Tests use deterministic fake executables, not paid requests.
Actual provider availability, authentication, model compatibility, and live resume behavior still require separately authorized integration testing.

Claude receives `-p --output-format json`, explicit `--model` and `--effort`, `--permission-mode dontAsk`, and matching `--tools` and `--allowedTools` lists from the validated read-only subset of `Read,Glob,Grep`.
An empty configured list disables built-in tools rather than restoring defaults.
`--settings '{}'` suppresses the installed wrapper's global model pin, while `--setting-sources ''` excludes user/project/local settings and their recursive orchestration hooks.
`--strict-mcp-config` excludes inherited MCP servers, and `--disable-slash-commands` disables skills.
Managed policy can still apply; these flags are not a permission bypass or an operating-system sandbox.
The dollar limit is passed as `--max-budget-usd`.
A saved conversation uses `--resume ID` with the same explicit model, effort, and permission controls.
The runtime must set the subprocess working directory to `cwd`; Claude has no corresponding cwd argument here.
Plain version: Claude can use only the configured reading tools, cannot ask for approval, and is told which model and spending limit to use.

Codex receives `-a never exec -s read-only -C CWD -m MODEL -c 'model_reasoning_effort="EFFORT"' --json --output-last-message PATH -- PROMPT`.
The effort value is encoded with `json.dumps` after config validation, producing a quoted TOML-compatible string rather than executable text.
Resumes retain the outer execution policy and use `exec ... resume --json --output-last-message PATH -- ID PROMPT`.
The positional API remains available for small test calls and rejects a literal `-` prompt.
Production runtime sets `stdin_prompt=True`: Claude reads the private prompt file from stdin, and Codex receives the explicit `-` stdin sentinel.
This avoids argument-size limits for large monitor batches.
Codex also receives `--ignore-user-config --ignore-rules` so inherited integrations do not widen the specialist role.
Plain version: prompt text is sent as text, not run as a shell command.

Codex has no equivalent to Claude's `Read,Glob,Grep` tool allowlist.
Its read-only sandbox can run shell-based inspection; `allowed_tools` does not disable individual Codex tools or shell commands.
User configuration and rules are deliberately excluded from specialists; the project-specific role contract and operator-managed policies define their authority.
Authentication environment and administrator policy still apply, and filesystem sandboxing alone does not confine every external service.
Codex has no supported dollar-cap flag here: `max_budget_usd` is not enforced for Codex, and token counts are not converted into an invented price.
Runtime must expose that limitation, and must refuse Codex dispatch if its policy requires an enforceable dollar cap or a Claude-equivalent tool allowlist.
Plain version: Codex is told not to write project files, but that does not limit every outside service or guarantee a spending limit.

## Normalized results

`parse_result(adapter, stdout, returncode)` returns exactly `{"text": str, "session_id": str, "cost_usd": number | None}` or raises `AdapterError`.
A zero process exit status alone never proves completion.
Nonzero exits, signals, invalid exit-status types, malformed JSON, duplicate JSON keys, missing terminal success, missing session IDs, conflicting session IDs or terminal results, and explicit failures or interruptions are rejected.
Identical terminal replay is tolerated; conflicting terminal replay is not.
Unknown informational event types are ignored for forward compatibility, never promoted to successful completion.
Plain version: the program must both exit cleanly and say it finished successfully.

Claude accepts a single JSON result object, including pretty-printed JSON, or JSONL containing a result.
A terminal must have `type=result`, `subtype=success`, and literal `is_error=false`.
`structured_output`, when present, is serialized as JSON into `text` in preference to `result`, including a legitimate JSON null value.
Otherwise `result` must be a nonempty string.
The consumer still validates the graph or review application schema; this parser does not interpret approval from prose.
Claude's `total_cost_usd` is optional, finite, numeric, nonnegative, and never a boolean or string.
Missing/null cost remains `None`, not zero.
Resumed cost scope is not yet verified; the runtime stores the reported value and does not aggregate it as verified per-turn spending.
Plain version: use the checked structured answer when provided, and leave unknown costs unknown.

Codex requires a `thread.started` ID, a completed `agent_message` with text, and a later `turn.completed`.
Only completed agent messages count; progress, reasoning, and tool output are not final answers.
The last completed agent message before terminal success supplies `text`.
Failure events, failed/interrupted items, and new turn/item activity after terminal completion are rejected conservatively.
The parser does not require or read `--output-last-message`; stale or missing artifact files cannot substitute for a successful event stream.
Codex `cost_usd` is always `None`, because the documented stream reports token usage rather than dollar cost.
Plain version: take Codex's final finished answer, not an earlier update or a leftover file.

## Runtime responsibilities

Runtime owns subprocess creation with `shell=False`, explicit cwd and a child-marked environment, bounded stdout/stderr capture, private output paths, and resource reservations.
It must remove inherited nested-session markers and model/effort overrides without leaking credentials into logs, and isolate unsafe inherited harness integrations.
Production prompts use private stdin files rather than argv; sensitive prompts still remain in owner-readable local artifacts.
Neither builder nor parser changes process-global environment variables.

Runtime also owns deadlines, cancellation, process groups, termination escalation, reaping children, lease fencing, and persistence.
Record the run before launch, reserve resources before spawning, and keep the reservation until the process tree is gone.
On cancellation or timeout, invalidate the attempt before accepting late results; terminate the whole group, wait a bounded grace period, then kill and reap remaining children.
Never report a canceled attempt as successful merely because stdout already contains a successful result.
A frontend disconnect is not cancellation, and an uncertain interrupted run must not be blindly replayed.
Plain version: the supervisor stops all parts of a canceled job and ignores answers that arrive too late.
