# Archived model research

This is the source research, not a benchmark of this implementation.
The selected initial defaults are Sonnet 5.5 low for the orchestrator, Opus 5.5 high for planner and monitor, and GPT-6 Astra high for the critic.
Fable remains a deeper-audit candidate, not the default monitor.
The later implementation deliberately removes per-role dollar budgets; spending-limit recommendations in this archived research are not current configuration.
See `../../config/default.toml` for executable role settings.

# Publicly verified model choices for multiagent orchestration

Research date: 2026-10-01 (task date).
Method: unauthenticated `curl -L` requests to live official documentation and benchmark publishers, with HTML text extraction using Python's standard library.
No provider API inference calls, private credentials, local configuration, or repository changes were used.
All names below come from pages actually retrieved, not extrapolated releases.
A catalog listing establishes documented availability, not access for a particular account, region, cloud, or usage tier.
No end-to-end orchestration benchmark was run.

## Executive recommendation

Start evaluation with `claude-sonnet-5-5` as the user-facing coordinator, `claude-opus-5-5` at xhigh effort as the complex planner, `gpt-6-astra` at high/xhigh as the different-provider critic, `claude-fable-5-1` at high/xhigh as the asynchronous deep monitor, and `gpt-6-luna` with no reasoning as the cheap extraction worker.
These are role-fit hypotheses, not a claim that these models are universally best.
Prioritize correct dispatch, state tracking, error recovery, and cancellation at low effort over the smallest model or cheapest token rate.
The main coordinator fallback is `gpt-6.1-sol` at low effort; `gemini-3.8-flash` at low thinking is another challenger.
Reserve `gpt-6-luna` with no reasoning for narrowly constrained routing only after it passes the actual coordination tests.
Use `gemini-3.5-flash-lite` at minimal thinking as the main alternative for multimodal extraction and Google-grounded search.

Plain version: use a quick model to talk and assign work, stronger models to solve and check hard problems, and a cheap model for simple reading tasks.
Do not make the user wait for every checker before showing progress.

## Verified official catalogs and capabilities

Prices are public standard input/output USD per million tokens, before caching, batch or priority pricing, search/tool fees, and any long-context price adjustments.
Context is a supported upper limit, not proof of accurate reasoning over the entire window.

### Anthropic

Source: https://platform.claude.com/docs/en/about-claude/models/overview

| Exact Claude API ID | Input / output | Context / maximum output | Official description and thinking |
| --- | --- | --- | --- |
| `claude-fable-5-1` | $10 / $50 | 1M / 128K | Demanding reasoning and long-horizon agentic work; slower; adaptive thinking always on; high default effort |
| `claude-opus-5-5` | $4 / $20 | 1M / 128K | Long-running agentic coding and knowledge work; moderate latency; adaptive thinking always on; medium default effort |
| `claude-sonnet-5-5` | $2 / $10 | 1M / 128K | Vendor calls it the best speed/intelligence combination; fast; adaptive thinking; high default effort |
| `claude-haiku-4-5-20251001` | $1 / $5 | 200K / 64K | Vendor's fastest current Claude; extended thinking; effort parameter unsupported |

The catalog explicitly says all current models support text and image input, text output, multilingual capabilities, vision, and tool use.
`claude-haiku-4-5` is the listed alias; the dated ID above is the catalog's Claude API ID.
Other providers have different naming conventions, so do not copy the Claude API IDs into Bedrock without checking that platform's model access and ID format.

Important operational evidence:

- https://platform.claude.com/docs/en/models/sonnet-5-5/whats-new-sonnet-5-5 states: to turn off up-front thinking, send `thinking: {"type": "between_tools"}`, not `disabled`.
  It accepts low, medium, or high effort, but not xhigh/max; changing effort mid-conversation in this mode returns an error.
  `disabled` returns HTTP 400.
  This is not a promise of zero thinking: the model can produce thinking/progress blocks between tools.
- The same page states that forced `tool_choice` types `any` and `tool` return HTTP 400; `auto` and `none` are supported.
  It recommends `auto` with strict tool use for schema-valid inputs.
  Strict schemas do not guarantee choosing the right tool or performing the correct action.
- Sonnet's default adaptive mode returns between-tool notes as thinking blocks with omitted text unless the application requests an appropriate display mode.
  An existing interface can therefore go quiet without an API error.
  In `between_tools` mode, summary text comes back; pass thinking blocks back unchanged.
- https://platform.claude.com/docs/en/build-with-claude/effort verifies low/medium/high/xhigh/max for Fable 5.1 and support for xhigh/max on Opus 5.5 and Sonnet 5.5.
  Exact wording: “Effort is a behavioral signal, not a strict token budget.”
  The hard `max_tokens` limit includes thinking plus response text.
- https://platform.claude.com/docs/en/models/fable-5-1/overview verifies Fable's prices and public model ID.
  It distinguishes `Claude Mythos 5.1`, which offers the same capabilities only to invited Project Glasswing participants.
  Do not recommend Mythos as generally available.
  Fable 5.1 also rejects forced tool use, and its thinking blocks are model/conversation-bound.
- The current catalog recommends starting with Opus 5.5, but Fable's introductory paragraph still says Opus 5.
  Treat that paragraph as stale cross-reference wording rather than evidence against the newer catalog entry.
- The catalog says Haiku retirement is “Not sooner than October 15, 2026.”
  That is not a confirmed retirement date, but its short remaining guaranteed window deserves checking before a new long-lived deployment.

Plain version: Sonnet can begin quickly, but the application must use its new settings correctly.
Old settings can fail or hide progress, and the model cannot be forced to call a particular tool using the old option.

### OpenAI

Sources:

- https://developers.openai.com/api/docs/models
- https://developers.openai.com/api/docs/models/gpt-6-astra
- https://developers.openai.com/api/docs/models/gpt-6.1-sol
- https://developers.openai.com/api/docs/models/gpt-6-luna

| Exact API ID | Input / output | Catalog context / maximum output | Supported reasoning settings |
| --- | --- | --- | --- |
| `gpt-6-astra` | $10 / $50 | 1.05M / 128K | low, medium, high, xhigh, max |
| `gpt-6.1-sol` | $2 / $10 | 1.05M / 128K | low, medium, high, xhigh, max |
| `gpt-6-luna` | $0.10 / $0.50 | 1.05M / 128K | none, low, medium, high, xhigh, max |

Extracted catalog wording: Astra is for “complex reasoning and coding”; Sol balances “intelligence and cost”; Luna is for “cost-sensitive, high-volume workloads.”
These are provider positioning statements, not independent comparative findings.
The catalog lists function tools, web search, file search, and computer use for all three and says latest models support text/image input and text output via the Responses API.
Luna's dedicated page verifies streaming, function calling, structured outputs, and Responses API support for hosted shell, MCP, tool search, code interpreter, and other tools.
Its page lists no free usage tier and gives usage-tier-dependent rate limits.
Its snapshot section says to use `gpt-6-luna`; no dated snapshot is asserted here.
Do not invent snapshot IDs for the other models either.

Luna is the only one of these three whose current catalog lists `none` reasoning.
Astra/Sol at low effort should not be described as non-reasoning models.
Actual supported tool combinations, pricing, and account limits still require deployment checks.
The installed Pi/provider catalog may lag these live model releases, and a listed provider integration may not yet support new effort levels, thinking modes, or response blocks.
This research did not inspect or verify the installed Pi catalog.
Verify the model ID and adapter support before deployment rather than assuming a documentation listing makes it selectable locally.

Plain version: Luna can skip extra thinking and costs much less.
Astra and Sol are stronger-work candidates, but their lowest setting is still a thinking setting.

### Google Gemini Developer API

Sources:

- https://ai.google.dev/gemini-api/docs/models
- https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash
- https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite
- https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview
- https://ai.google.dev/gemini-api/docs/thinking
- https://ai.google.dev/gemini-api/docs/pricing

| Exact API ID | Verified status | Standard input / output | Thinking |
| --- | --- | --- | --- |
| `gemini-3.8-flash` | Stable | $0.75 / $3.75 through December 31, 2026; $1.50 / $7.50 from January 1, 2027 | low, medium, high; medium default; minimal returns an error |
| `gemini-3.5-flash-lite` | Stable | $0.30 / $2.50 | minimal, low, medium, high; minimal default |
| `gemini-3.1-pro-preview` | Preview | $2 / $12 at prompts <=200K; $4 / $18 above 200K | low, medium, high; high default |
| `gemini-3.1-pro-preview-customtools` | Preview | Same pricing entry as Pro Preview | Custom-tools-specialized variant; verify settings against its current endpoint |

The model pages list text/image/video/audio/PDF inputs, text output, function calling, structured outputs, and search grounding for Flash 3.8, Flash-Lite 3.5, and Pro 3.1 Preview.
Flash-Lite and Pro pages specify 1,048,576 input tokens and 65,536 output tokens.
Flash-Lite's official description explicitly mentions “subagent tasks and document parsing” and “simple data extraction.”
Pro's `customtools` variant is documented as better at prioritizing user tools such as `view_file` and `search_code` when combined with bash, with possible quality fluctuations on tasks that do not benefit from these tools.
Pro file search is listed as “Supported (AI Studio only)”; do not assume general API file-search parity.
Computer use on the Flash/Flash-Lite pages is marked Preview even though the models themselves are Stable.

Pricing includes thinking tokens in output.
The current Gemini 3.x search pricing text says 5,000 free search requests per month shared across Gemini 3.x, then $14 per 1,000 requests.
The pricing footnote says one customer request can trigger multiple billable search queries.
Search is therefore not priced solely by answer tokens; recheck exact billing units when implementing.
Paid and free tiers have different data-use terms: the pricing tables mark paid-tier data as not used to improve products and free-tier data as used.
A free tier is not a production capacity guarantee.

The catalog also contains audio/live/image-generation models, which are not interchangeable with the text orchestration models above.
Artificial Analysis lists “Gemini 4 Argon (high),” but no generally available exact API ID was verified from the official catalog in this pass, so it is excluded from recommendations.
The thinking guide contains a `gemini-3.8-pro` code example, but a stray example is insufficient to establish a public model release; it is also excluded.

Plain version: Flash-Lite is designed for cheap reading work.
Flash 3.8 cannot use the smallest thinking setting, Pro is still a preview, and search adds charges beyond the model's text bill.

## Independent evidence: speed, latency, intelligence, and tools

### Artificial Analysis

Sources:

- https://artificialanalysis.ai/leaderboards/models
- https://artificialanalysis.ai/methodology

The retrieved leaderboard headers are “Median Tokens/s,” “Latency First Chunk (s),” and “Total Response (s).”
Preserve those labels: first chunk is not necessarily first useful user-facing text or a complete usable tool call.
The methodology separately defines time to first token and time to first answer token, noting that TTFT can be the first reasoning token when a model streams reasoning.
Output speed is measured after the first token.
The methodology says reasoning-token estimates use a 60-prompt sample, with a 2K assumption where an average is not yet available.
These are benchmark measurements/estimates, not p95 production service guarantees.

Selected exact rows from the retrieved page:

| Model/configuration label | Intelligence Index | Median tokens/s | First chunk, seconds | Total response, seconds |
| --- | --- | --- | --- | --- |
| Claude Sonnet 5.5 (low with fallback) | not reported | 88 | 1.17 | 6.84 |
| Claude Sonnet 5.5 (medium with fallback) | 41 | 92 | 1.34 | 6.80 |
| GPT-6 Luna (non-reasoning) | 18 | 132 | 0.81 | 4.59 |
| GPT-6 Luna (low) | 21 | 134 | 2.45 | 6.18 |
| GPT-6.1 Sol (low) | 42 | 60 | 2.61 | 10.99 |
| Gemini 3.5 Flash-Lite | 22 | 339 | 9.32 | 10.79 |
| Gemini 3.8 Flash (high) | 41 | 236 | 21.76 | 23.89 |
| Claude Opus 5.5 (high with fallback) | 54 | 74 | 37.37 | 44.09 |
| Claude Opus 5.5 (xhigh with fallback) | 56 | 79 | 137.34 | 143.70 |
| Claude Opus 5.5 (max with fallback) | 58 | 92 | 702.55 | 707.98 |
| Claude Fable 5.1 (high with fallback) | 51 | 51 | 27.40 | 37.21 |
| GPT-6 Astra (xhigh) | 52 | 48 | 142.28 | 152.79 |

Cautions:

- The Claude labels explicitly include “with fallback.”
  They must not be presented as isolated base-model results or assumed to reproduce Sonnet's `between_tools` configuration.
  The fallback mechanism was not independently inspected in this pass.
- The high-effort rows show why the strongest benchmark configuration may make a poor interactive coordinator.
  Opus max's measured first-chunk value is over eleven minutes in this table, not a general prediction for every request.
- Flash-Lite's high generation speed does not translate into a low first-chunk value in this benchmark row.
  It would be misleading to call it the fastest conversational coordinator solely from its tokens/s number.
- Flash 3.8 low/medium rows had missing performance measurements, so no latency extrapolation from the high row is warranted.
- The intelligence index measures a mixture of tasks, not the precise coordinator workflow.
  The current page's intelligence order does not establish a universal planner or monitoring winner.
- Context length, tokenization, region, provider endpoint, caching, concurrent load, model effort, and response size all change observed latency and cost.

Plain version: a model can write very fast after waiting a long time before it starts.
The numbers favor testing Luna and low-effort Sonnet for responsiveness, but they do not prove either will finish your tool jobs fastest.

### Berkeley Function Calling Leaderboard (BFCL)

Source: https://gorilla.cs.berkeley.edu/leaderboard.html

Verified extracted evidence: BFCL V4 evaluates accurate function calling and introduces holistic agentic evaluation; its page says “Last Updated: 2026-04-12.”
It distinguishes native function calling from prompt-based emulation, describes overall accuracy as the unweighted average of subcategories, and identifies evaluation commit `f7cf735` and `bfcl-eval==2025.12.17`.
The static HTML retrieved here did not include the dynamic score rows, so no numerical BFCL rankings are claimed.
More importantly, its displayed update predates the newest model catalog entries.
Do not cite BFCL as proof that these exact October candidates have superior tool reliability.

Function-schema correctness, tool selection, multi-turn state handling, and recovery from errors must be tested separately.
A correct JSON call can still target the wrong account or perform the wrong action.

Plain version: this benchmark checks important tool skills, but the page fetched here cannot tell us which of these newest models is best at them.

### Sierra tau-bench family

Source: https://taubench.com/
Repository linked by publisher: https://github.com/sierra-research/tau2-bench

The live site says tau-bench measures conversation, tool calling, retrieval, and policy following across enterprise domains.
Its current homepage separates tau3-Banking, tau3-Voice, and tau2-bench rather than providing one interchangeable score.
The retrieved tau2 teaser reports Qwen3.5-397B-A17B 87.9%, Gemini 3.0 Pro 85.4%, and Claude Opus 4.5 85.3% Pass^1.
Those are benchmark model labels, not API IDs or evidence that current Sonnet/Astra/Flash versions share those scores.
The site also documents February 2026 fixes to more than 50 tasks; version differences therefore matter.

Pass^1 is single-attempt success, not a guarantee that repeated attempts succeed consistently.
Compare the same benchmark version, user simulator, tool harness, prompts, and retry budget; ask for repeated-run consistency, policy violations, and failure categories as well as aggregate success.
Sierra publishes the benchmark but also operates in the agent industry; “independent” here means not the model vendor's marketing table, not absence of every commercial interest.

Plain version: completing a customer-service test once does not mean a model will complete it safely every time, or handle your code agents equally well.

## Role-specific settings, alternatives, and conditions

### Responsive coordinator

Primary evaluation candidate: `claude-sonnet-5-5`, `thinking: {"type":"between_tools"}`, fixed low or medium `output_config.effort`, streaming enabled, short replies, and a small explicitly described dispatch toolset.
Use the host application to execute actual `spawn_agent`, cancel, status, and wait operations; the model only requests tool calls.
Do not expect “supports tools” to mean the model itself runs independent background agents.
Validate tool arguments, budgets, dependencies, and authorization in code.
If mandatory dispatch is required, enforce it in host logic rather than using Sonnet's unsupported forced-tool options.

Fallback candidates: `gpt-6.1-sol` at low effort first, and `gemini-3.8-flash` at low for a Google-native stack.
Use `gpt-6-luna` with `reasoning.effort: "none"` only for narrowly constrained routing after it passes the same state-management and recovery tests.
Do not downsize the coordinator solely because Luna is cheaper or has a lower first-chunk number.
A very simple dispatcher may not need an LLM at all.
Escalate complex interpretation to the planner rather than increasing coordinator thinking on every turn.

Plain version: keep the talking model's job small, and let code enforce what it is allowed to start.

### Complex high-effort planner

Start with `claude-opus-5-5` at xhigh; use max only where measured solution-quality gains justify potentially much longer waits.
Alternative `claude-fable-5-1` at high/xhigh follows Anthropic's positioning for especially demanding long-horizon tasks, but its higher price and current benchmark mixture do not establish a universal quality win over Opus.
Alternative `gpt-6-astra` at high/xhigh is a different-provider planner.
Use written requirements, constraints, explicit uncertainties, dependency ordering, and checkable acceptance criteria.
Pass plan artifacts, not hidden reasoning state, between providers.

Plain version: ask the planner for steps you can check, and spend more time thinking only when that improves the result.

### Different-model critic

For an Anthropic planner, use `gpt-6-astra` at high/xhigh; use `gpt-6.1-sol` at high for a lower-cost alternative.
For an OpenAI planner, use `claude-opus-5-5` at high/xhigh.
`gemini-3.1-pro-preview` is a third-provider alternative if preview risk and its actual task performance are acceptable.
Give the critic the original requirements and raw evidence, not just the planner's persuasive summary, and request counterexamples, missing constraints, and executable checks.

Cross-provider review does not guarantee statistical independence: training data, public benchmarks, optimization targets, prompt framing, and factual misconceptions can overlap.
If both agents receive the same wrong evidence, both can agree incorrectly.
Blind initial review, varied testing strategies, and deterministic validation matter more than simply changing the model name.

Plain version: two different companies' models can still make the same mistake.
Make the second model check the facts and tests itself before showing it the first model's conclusion.

### Slow strong monitor

Use `claude-fable-5-1` at high/xhigh for periodic semantic audits of progress, invariants, scope drift, and evidence quality; alternatively `gpt-6-astra` high/xhigh adds provider diversity to an Anthropic-heavy system.
`claude-opus-5-5` high is a lower-priced strong alternative if it passes the same monitoring tests.
This role recommendation is based on documented capability fit, not a public monitoring-specific leaderboard.
Trigger it on milestones, failure clusters, or scheduled summaries, not every token or filesystem event.
A deterministic watchdog must independently enforce hard deadlines, spending limits, liveness, and cancellation.
A slow model must never be the only emergency stop or health check.

Plain version: the strong model checks whether work is going wrong, while ordinary code stops runaway jobs immediately.

### Cheap extraction and search

Use `gpt-6-luna` with none reasoning for schema-constrained extraction and classification, with evidence spans and validation.
Use `gemini-3.5-flash-lite` minimal for multimodal parsing and Google-grounded search where its capabilities help; its text-token price is higher than Luna's, so it is not automatically the cheapest choice.
Haiku 4.5 is an Anthropic-only alternative, not the lowest-token-price option among the verified candidates.
Prefer deterministic parsing or an existing search index when those solve the task reliably.
Treat extraction and retrieval as different operations: a cheap extractor does not itself guarantee current facts or search recall.

Plain version: use cheap models to read and format evidence, but make sure real search tools supply the evidence first.

## What to measure before choosing

1. Time to first stream event, first visible answer text, and first complete actionable tool call, separately.
2. Thinking time and reasoning-token consumption, including hidden or summarized reasoning where usage metadata is available.
3. Complete tool cycle: model scheduling plus complete argument generation plus transport/queueing plus tool execution plus the next model turn.
4. End-to-end completion time, p50/p95/p99, under realistic concurrency and prompt/history lengths.
5. Output tokens/s separately from requests/s or completed workflows/s at quota.
6. Valid tool schema rate, correct dispatch, unauthorized action attempts, retries, idempotency violations, missed cancellations, and recovery after tool failures.
7. Total cost per successfully completed workflow: repeated prompts, cached tokens, reasoning/output, child agents, reviewer/monitor calls, tool/search fees, failed runs, and retries.

Use identical tool schemas and traces, but provider-native settings rather than pretending every provider has equivalent effort values.
Include prompt injection in retrieved documents, malformed tool results, duplicate events, quota exhaustion, timeouts, stale agent status, and concurrent state updates.
Reserve capacity for the coordinator separately from batch workers so background work cannot starve the user interface.
Keep the coordinator's state compact and preserve the full audit log outside its prompt.
No authenticated benchmark was performed in this research, so production reliability and tool-roundtrip latency remain unverified.

Plain version: time the whole job, count wrong actions and total bills, and test what happens when tools fail.
A fast first word is not enough.

## Evidence files and final decision

Raw catalog fetches: `/tmp/claude-models.html`, `/tmp/openai-models.html`, `/tmp/google-models.html`, with corresponding `.txt` extractions.
Detailed model and benchmark fetches use `/tmp/research-*.html` and `/tmp/research-*.txt`; later supplementary fetches retain extracted text only.
These are reproducibility aids, not immutable published archives.

Start with Sonnet for coordination, Opus for planning, Astra for criticism, Fable for periodic deep monitoring, and Luna for cheap extraction, then run your own tool traces before locking the choices.
Make low-effort Sol the first coordinator fallback candidate, not the smallest available model.
If Google-native search or multimodal parsing matters, add Flash-Lite for extraction and low-thinking Flash 3.8 for coordination to that evaluation.
These choices require local tool-workflow tests and compatible installed provider adapters; they are not a measured orchestration ranking.
