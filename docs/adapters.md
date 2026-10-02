# Supervised execution adapters

`orchestrator.adapters.build_command` builds specialist argv without launching a process or writing files.
`orchestrator.worker_execution.build_worker_command` builds worker argv with read or write authority.
Claude Code executes Anthropic models only; all other models use the owned Pi SDK bridge, not direct Codex CLI execution.
The critic uses Pi provider `openai-codex`, model `gpt-6-astra`, and effort `high`.
Plain version: the configured model determines which approved program runs the task, without substituting another model.

## Claude Code

Specialists receive `-p --output-format json`, explicit `--model` and `--effort`, `--permission-mode dontAsk`, and matching `--tools` and `--allowedTools` lists.
The specialist allowlist is a validated subset of `Read,Glob,Grep`; an empty list disables tools rather than restoring defaults.
Workers additionally receive `--restricted`, with `Read,Glob,Grep` for reading and `Edit,Write` added only in write mode.
Restricted workers receive file tools by default; `commands.enabled` adds owned `run_command`, which uses the OS sandbox unless `commands.sandbox = false`.
Trusted workers receive owned `run_command` automatically, always without an OS sandbox.
Native permission prompts are not automatically approved.
Write workers are not granted the source checkout as an additional directory.

`--settings '{}'` suppresses the installed wrapper's global model pin, while `--setting-sources ''` excludes inherited settings and recursive orchestration hooks.
`--strict-mcp-config` excludes inherited MCP servers, and `--disable-slash-commands` disables skills.
These controls are not an operating-system sandbox or a bypass of managed policy.
Claude specialist resume uses an explicit saved conversation ID through `--resume`, with the same model selector, effort, and permissions.
The selector may be an exact ID or a canonical native family alias; a family can resolve differently on a later launch.
Worker invocations start fresh instead of inheriting a conversation.
Plain version: workers get the listed file tools; unsandboxed commands can act outside those file-tool limits.

## Owned Pi SDK bridge

Pi execution is pinned to `@earendil-works/pi-coding-agent` version **0.99.2**.
The configured Pi executable identifies the installed package; it is not launched as an unrestricted CLI worker.
An isolated Python launcher in `orchestrator/pi_tools.py` validates that installation outside model-accessible roots, then starts `orchestrator/pi_bridge.mjs` through a trusted Node executable.
The bridge imports the SDK from that installation and validates its package name and version again.

Provider, model, effort, and tools are explicit.
The bridge resolves exact IDs first, or supported families such as `Astra` and `Sol` within the installed provider catalog, then requires usable authentication, supported effort, and an unchanged session effort and active-tool allowlist.
Pi worker `max-supported` selects the highest effort supported by the resolved model; core roles still require an explicit supported effort.
An unavailable model, unsupported effort, changed model, or failed preflight stops the task without silent fallback or effort downgrade.
Pi executor efforts are `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`, subject to the chosen model's capabilities.
Schema validation does not prove account access; live model availability still requires authorized testing.
Plain version: if Pi cannot use exactly what was requested, it stops instead of choosing something else.

Every Pi task has a fresh in-memory session and temporary agent directory.
Pi tasks are ephemeral and cannot resume; their diagnostic session header is not a resumable supervisor session ID.
The bridge disables inherited extensions, skills, prompt templates, agent files, user/project model catalogs, automatic compaction, and retries.
It installs owned file tools, never the default shell tool.
Trusted workers additionally receive owned `run_command`; registered team peers receive durable send/read message tools, and enabled image generation uses the owned image tool.

Authentication reuses the canonical resolved `auth.json` path from `PI_CODING_AGENT_DIR`, or the usual `~/.pi/agent` directory when unset.
The temporary task home does not receive an auth copy or alias.
Canonical storage locking and OAuth writeback remain in use, access is filtered to the selected provider, and command-valued credentials are rejected before resolution.
Only selected provider environment variables are forwarded; inherited plugins and runtime injection settings are not.
Plain version: Pi can use your existing login without loading your usual extra tools or making a separate login file for each worker.

## File tools and their limits

Pi reading tools are `read`, `ls`, `find`, and `grep`; write mode adds `edit` and `write`.
The broker handles bounded UTF-8 text files with descriptor-relative access.
Restricted tools reject symlinks; trusted tools resolve ordinary internal aliases and recheck that the resolved path stays inside the permitted roots.
Repository `AGENTS.md` and `CLAUDE.md` files are readable.
Trusted workers can also read and edit ordinary project configuration such as `.gitignore`, `.github`, and instruction files.
Credential files, orchestration control state, Git metadata, paths outside authorized roots, and multiply linked or special files remain excluded from these file tools.
An individual file access error is recoverable: the model can correct its path and continue without restarting the task.
Writes are confined to a separate worker checkout; the source project is readable but not writable through these tools.
`grep` uses literal text, not regular expressions.
Command-enabled restricted workers use the OS sandbox unless `commands.sandbox = false`; trusted workers can build and test with host access.
Trusted commands receive host toolchain paths and the user home for installed tools and Git identity, but API-key environment variables are not copied into commands.
Trusted and `commands.sandbox = false` command execution is unsandboxed and does not inherit file-tool path containment.
These are model-tool controls, not hostile-process isolation or an OS sandbox.
Plain version: file tools only read or edit allowed files, while unsandboxed commands can do more and must be used with care.

## Normalized results

`parse_result(adapter, stdout, returncode)` returns `text`, `session_id`, and `cost_usd`, or raises `AdapterError`.
A clean process exit alone is insufficient.
Malformed JSON, duplicate keys, explicit failures, missing completion, and conflicting terminal results are rejected.

Claude requires terminal `type=result`, `subtype=success`, literal `is_error=false`, and a consistent session ID.
If present, `structured_output` is serialized into `text`; otherwise the terminal result must contain nonempty text.
Optional Claude dollar cost must be finite and nonnegative; missing cost remains unknown.
Optional `modelUsage` contributes a bounded `reported_models` list, kept separately from the requested selector in the fenced completion event.
This list can include auxiliary calls and is not evidence of one uniquely identified primary model.
Its resumed-session cost scope is not established, so reported values must not be summed as verified per-turn spending.

Pi requires a verified 0.99.2 preflight, a diagnostic session header, matched message and tool events, a successful final assistant message, and settlement with no later activity.
Ordinary tool errors may be followed by corrective calls and a successful final result.
Unoffered tools, broken protocol, retries, compaction, model changes, unexpected events, incomplete tools, and truncated output still reject the result.
Preflight failures retain an actionable owned diagnostic instead of reporting only an exit code.
Pi returns `session_id=None` and `cost_usd=None`.
A legacy Codex transcript decoder remains for compatibility, but direct Codex execution is not supported.
Plain version: an answer counts only after the expected program finishes cleanly with the requested model and tools.

## Runtime responsibilities

The runtime owns subprocess creation, explicit cwd, private stdin prompts, bounded output, resource reservations, deadlines, cancellation, process identity, attempt fencing, and persistence.
Production prompts travel through stdin rather than command arguments, but remain in owner-readable local artifacts.
Neither builder nor parser mutates the process-global environment.
A frontend disconnect is not cancellation; cancelled or uncertain attempts are not silently replayed or accepted.
No per-role dollar cap is configured.
Worker results additionally require structured reports and, for writes, program-captured workspace provenance before becoming candidates.
Checked acceptance through `accept_worker`, the optional CLI, or standing `execution.unattended` authorization completes graph nodes; see [workers](workers.md).
Unattended acceptance requires an explicit empty `remaining_issues` list and satisfied live gates, and never grants required worker approval.
Plain version: the supervisor records each job and accepts it only under the project's authorization rules.
