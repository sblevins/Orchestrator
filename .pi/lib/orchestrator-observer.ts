import { createAssistantMessageEventStream, getCurrentTools, type AssistantMessage } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { readFile, realpath } from "node:fs/promises";
import { resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";

export const OBSERVER_PROVIDER = "orchestrator-local-observer";
export const OBSERVER_MODEL = "worker-status";
export const OBSERVER_DEFINITION = `---
description: Display-only observer of a supervisor-owned Orchestrator worker
display_name: Worker observer
tools: none
extensions: false
skills: false
isolated: true
thinking: off
persist_session: false
output_transcript: false
prompt_mode: replace
---
Display existing worker status only; never execute a worker task.
Stopping this observer detaches its display and does not cancel the worker.
Steering is unsupported and cannot change worker execution.
Completion of observation is not approval or acceptance of the worker result.
`;
const MAX_OBSERVERS = 8;
const MAX_ATTEMPTS = 256;
const MAX_PAGES = 4;
const MAX_DURATION = 60 * 60 * 1000;
const MAX_TEXT = 32000;
const POLL_INTERVAL = 3000;

type Request = (action: string, payload?: Record<string, unknown>, signal?: AbortSignal) => Promise<any>;
interface Worker {
  request_id: string; task_id: string | null; state: string; task_state?: string;
  done: boolean; accepted: boolean; label?: string; report?: unknown;
  profile?: { harness?: string; provider?: string; model?: string; effort?: string };
}
interface Observation {
  requestId: string; taskId: string; token: string; generation: number;
  controller: AbortController; id?: string; used: boolean; timer: ReturnType<typeof setTimeout>;
}

/** Worker text is data, never terminal instructions or local-provider routing instructions. */
export function observerText(value: unknown, limit = 1000): string {
  const text = typeof value === "string" ? value : JSON.stringify(value) ?? "";
  return text.replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
    .replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]/g, "").slice(0, limit);
}
function workerFrom(value: any, expectedId?: string): Worker {
  if (!value || typeof value.request_id !== "string" || value.request_id.length > 200 ||
      (expectedId !== undefined && value.request_id !== expectedId) || typeof value.state !== "string" ||
      typeof value.done !== "boolean" || typeof value.accepted !== "boolean") throw new Error("Invalid worker view");
  return value;
}
function snapshot(worker: Worker): string {
  const profile = worker.profile;
  return `Worker ${observerText(worker.request_id)}: ${observerText(worker.label ?? "")}\n` +
    `Actual worker: ${observerText(profile?.harness ?? "unknown")} / ${observerText(profile?.provider ?? "unknown")} / ` +
    `${observerText(profile?.model ?? "unknown")} / effort ${observerText(profile?.effort ?? "unknown")}\n` +
    `Request: ${observerText(worker.state)}; task: ${observerText(worker.task_state ?? "unknown")}; ` +
    `accepted: ${worker.accepted ? "yes" : "no"}.\n`;
}

/** Public Pi provider + pi-subagents RPC only. Never owns or cancels durable workers. */
export function createWorkerObservers(pi: ExtensionAPI, root: string, request: Request) {
  let generation = 0;
  let context: ExtensionContext | undefined;
  let pluginReady = false;
  let syncing = false;
  let offset = 0;
  let status = "Waiting for the installed pi-subagents plugin; no observer attached";
  const observations = new Map<string, Observation>();
  const capabilities = new Map<string, Observation>();
  // Bounded tombstones: stopped/failed/finished observers never automatically respawn.
  const attempted = new Set<string>();
  const pending = new Set<() => void>();
  // Separate from active capacity. Drain at most eight late spawn replies for 30 seconds,
  // including across stop/start; unknown children have already lost their capabilities.
  const lateReplies = new Set<() => void>();
  let discovery: Promise<boolean> | undefined;

  function publish(message: string) {
    status = message;
    if (context?.hasUI) context.ui.setStatus("orchestrator-observers", observerText(message, 240));
  }
  function stopNative(id: string) {
    try { pi.events.emit("subagents:rpc:stop", { requestId: randomUUID(), agentId: id }); }
    catch { /* Best effort if the plugin is unloading; local capabilities are already revoked. */ }
  }
  function release(observation: Observation, detach = false) {
    clearTimeout(observation.timer);
    observation.controller.abort();
    capabilities.delete(observation.token);
    if (observations.get(observation.requestId) === observation) {
      observations.delete(observation.requestId);
      if (observation.generation === generation) publish(`${observations.size} active local observer(s). Finished/detached observers require explicit reattachment; workers unchanged.`);
    }
    if (detach && observation.id) stopNative(observation.id);
  }
  function rpc(method: string, payload: Record<string, unknown>, version: number, onCancel?: () => void): Promise<any> {
    return new Promise((resolveReply, rejectReply) => {
      let expired = false;
      let lateTimer: ReturnType<typeof setTimeout> | undefined;
      const requestId = randomUUID();
      const cleanup = () => {
        clearTimeout(timer);
        clearTimeout(lateTimer);
        unsubscribe(); pending.delete(cancel); lateReplies.delete(cleanup);
      };
      const unsubscribe = pi.events.on(`${method}:reply:${requestId}`, (raw: any) => {
        cleanup();
        if (expired || generation !== version) {
          if (method.endsWith(":spawn") && raw?.success && typeof raw.data?.id === "string") stopNative(raw.data.id);
          rejectReply(new Error("Native observer unavailable or detached"));
          return;
        }
        raw?.success ? resolveReply(raw.data) : rejectReply(new Error("Native observer RPC failed"));
      });
      const cancel = () => {
        if (expired) return;
        expired = true;
        clearTimeout(timer);
        pending.delete(cancel); // Lost replies must never retain active request capacity.
        onCancel?.(); // Revoke authorization and abort any local stream immediately.
        if (method.endsWith(":spawn")) {
          while (lateReplies.size >= MAX_OBSERVERS) lateReplies.values().next().value!();
          lateReplies.add(cleanup);
          lateTimer = setTimeout(cleanup, 30000);
          lateTimer.unref?.();
        } else cleanup();
        rejectReply(new Error("Native observer unavailable or detached"));
      };
      const timer = setTimeout(cancel, 1500);
      pending.add(cancel);
      try { pi.events.emit(method, { requestId, ...payload }); }
      catch { cancel(); }
    });
  }
  async function discover(): Promise<boolean> {
    if (!context) return false;
    if (pluginReady) return true;
    if (discovery) return discovery;
    const version = generation;
    discovery = (async () => {
      try {
        const reply = await rpc("subagents:rpc:ping", {}, version);
        if (version !== generation) return false;
        pluginReady = reply?.version === 2;
        publish(pluginReady ? "Local worker observers available in /agents (final results; live Fleet not guaranteed)" : "Unsupported pi-subagents protocol; no observer attached");
      } catch {
        if (version === generation && !pluginReady) publish("pi-subagents unavailable; no native observer attached (nothing installed)");
      }
      return version === generation && pluginReady;
    })();
    return discovery;
  }
  pi.events.on("subagents:ready", () => {
    discovery = undefined;
    if (context) void discover();
  });
  for (const name of ["subagents:completed", "subagents:failed"]) {
    pi.events.on(name, (raw: any) => {
      for (const observation of observations.values()) {
        if (observation.id === raw?.id) {
          release(observation);
          publish("Observer finished/detached; worker unchanged. Use /orchestrator-observe REQUEST_ID to reattach.");
        }
      }
    });
  }

  pi.registerProvider(OBSERVER_PROVIDER, {
    api: "orchestrator-local-worker-status", baseUrl: "http://127.0.0.1.invalid", apiKey: "local-observation-only",
    models: [{ id: OBSERVER_MODEL, name: "Local worker observer (no LLM)", reasoning: false,
      input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 1000000, maxTokens: 16000 }],
    streamSimple(model, transcript, options = {}) {
      const stream = createAssistantMessageEventStream();
      const message: AssistantMessage = { role: "assistant", api: model.api, provider: model.provider, model: model.id,
        timestamp: Date.now(), stopReason: "pending", content: [],
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
      void (async () => {
        let observation: Observation | undefined;
        let abort: (() => void) | undefined;
        try {
          const firstUser = transcript.messages.find((entry) => entry.role === "user");
          const token = firstUser?.role === "user"
            ? typeof firstUser.content === "string" ? firstUser.content
              : firstUser.content.length === 1 && firstUser.content[0].type === "text" ? firstUser.content[0].text : ""
            : "";
          observation = capabilities.get(token);
          if (!observation || observation.used || observation.generation !== generation ||
              model.provider !== OBSERVER_PROVIDER || model.id !== OBSERVER_MODEL ||
              (getCurrentTools(transcript.messages) ?? []).length) throw new Error("Unrecognized or reused display-only observation");
          observation.used = true;
          const current = observation;
          abort = () => current.controller.abort();
          options.signal?.addEventListener("abort", abort, { once: true });
          if (options.signal?.aborted) abort();
          const signal = current.controller.signal;
          const originalPayload = { request_id: current.requestId, observation_only: true };
          const replacement = await options.onPayload?.(originalPayload, model);
          if (replacement !== undefined && JSON.stringify(replacement) !== JSON.stringify(originalPayload)) throw new Error("Observer identity cannot be replaced");
          stream.push({ type: "start", partial: message });
          const block = { type: "text" as const, text: "" };
          message.content.push(block);
          stream.push({ type: "text_start", contentIndex: 0, partial: message });
          function emit(text: string) {
            const delta = text.slice(0, MAX_TEXT - block.text.length);
            block.text += delta;
            stream.push({ type: "text_delta", contentIndex: 0, delta, partial: message });
          }
          emit("DISPLAY-ONLY OBSERVER. No LLM call. Stop detaches this display, not the worker. Steering is unsupported.\n" +
            "Worker output is untrusted data, not instructions or approval. /agents shows the final result; live Fleet/text may be unavailable.\n");
          let previous = "";
          while (true) {
            signal.throwIfAborted();
            const result = await request("worker_view", { request_id: current.requestId }, signal);
            signal.throwIfAborted();
            if (current.generation !== generation) throw new Error("Observer detached");
            const worker = workerFrom(result.worker, current.requestId);
            if (worker.task_id !== current.taskId) throw new Error("Worker attempt changed; reattach explicitly");
            const text = snapshot(worker);
            await options.onProviderStreamEvent?.({ state: worker.state, task_state: worker.task_state, done: worker.done }, model);
            signal.throwIfAborted();
            if (text !== previous && block.text.length < MAX_TEXT - 14000) { emit(text); previous = text; }
            if (worker.done) {
              emit("Observation complete; supervisor approval and acceptance are unchanged.\n" +
                (worker.report === undefined ? "No public report available.\n" : observerText(worker.report, 12000) + "\n"));
              break;
            }
            await delay(POLL_INTERVAL, undefined, { signal });
          }
          stream.push({ type: "text_end", contentIndex: 0, content: block.text, partial: message });
          message.stopReason = "stop";
          stream.push({ type: "done", reason: "stop", message });
        } catch {
          message.stopReason = options.signal?.aborted || observation?.controller.signal.aborted ? "aborted" : "error";
          message.errorMessage = "Observation ended or unavailable; durable worker unchanged. Reattach explicitly with /orchestrator-observe REQUEST_ID.";
          stream.push({ type: "error", reason: message.stopReason, error: message });
        } finally {
          if (abort) options.signal?.removeEventListener("abort", abort);
          if (observation) release(observation);
          stream.end();
        }
      })();
      return stream;
    },
  });

  async function attach(worker: Worker, explicit = false): Promise<string | undefined> {
    if (!context || !worker.task_id || typeof worker.task_id !== "string") return;
    if (observations.has(worker.request_id)) return observations.get(worker.request_id)?.id;
    if ((!explicit && (worker.done || attempted.has(worker.request_id))) || observations.size >= MAX_OBSERVERS || pending.size >= MAX_OBSERVERS) return;
    if (!attempted.has(worker.request_id) && attempted.size >= MAX_ATTEMPTS) {
      publish("Observer session limit reached; reload to reset observation history"); return;
    }
    const version = generation;
    const selectedContext = context;
    if (!(await discover()) || version !== generation) return;
    const definition = resolve(selectedContext.cwd, ".pi/agents/orchestrator-observer.md");
    if (await realpath(selectedContext.cwd) !== await realpath(root) ||
        await realpath(process.cwd()) !== await realpath(root) ||
        await realpath(definition) !== definition || await readFile(definition, "utf8") !== OBSERVER_DEFINITION) {
      throw new Error("Observer definition differs from the owned tool-free definition; restore it and reload Pi");
    }
    const model = selectedContext.modelRegistry.find(OBSERVER_PROVIDER, OBSERVER_MODEL);
    if (!model || model.provider !== OBSERVER_PROVIDER || model.id !== OBSERVER_MODEL || model.api !== "orchestrator-local-worker-status") {
      throw new Error("Exact local observer model unavailable; no fallback permitted");
    }
    if (version !== generation || observations.has(worker.request_id) || observations.size >= MAX_OBSERVERS || pending.size >= MAX_OBSERVERS ||
        (!attempted.has(worker.request_id) && attempted.size >= MAX_ATTEMPTS)) return;
    attempted.add(worker.request_id);
    const observation: Observation = { requestId: worker.request_id, taskId: worker.task_id, generation: version,
      token: `orchestrator-observe:${randomUUID()}`, controller: new AbortController(), used: false,
      timer: undefined as unknown as ReturnType<typeof setTimeout> };
    observation.timer = setTimeout(() => release(observation, true), MAX_DURATION);
    observations.set(worker.request_id, observation);
    capabilities.set(observation.token, observation);
    try {
      const reply = await rpc("subagents:rpc:spawn", { type: "orchestrator-observer", prompt: observation.token,
        options: { description: `Observer: ${observerText(worker.request_id, 100)}`, model, isolated: true,
          inheritContext: false, thinkingLevel: "off" } }, version, () => release(observation, true));
      if (typeof reply?.id !== "string") throw new Error("Native observer returned no ID");
      observation.id = reply.id;
      if (version !== generation || observations.get(worker.request_id) !== observation) { stopNative(reply.id); return; }
      publish(`${observations.size} local worker observer(s) in /agents; stop detaches only. Live Fleet not guaranteed.`);
      return reply.id;
    } catch (error) { release(observation, true); throw error; }
  }
  return {
    start(ctx: ExtensionContext) {
      this.stop(); context = ctx; pluginReady = false; discovery = undefined;
      void discover();
    },
    stop() {
      generation++; context = undefined; syncing = false; offset = 0;
      for (const observation of [...observations.values()]) release(observation, true);
      for (const cancel of [...pending]) cancel();
      attempted.clear(); pluginReady = false; discovery = undefined;
    },
    async sync() {
      if (!context || syncing || !pluginReady || observations.size >= MAX_OBSERVERS) return;
      syncing = true;
      const version = generation;
      try {
        for (let page = 0; page < MAX_PAGES && observations.size < MAX_OBSERVERS; page++) {
          const result = await request("worker_view", { offset });
          if (version !== generation) return;
          if (!Array.isArray(result.workers) || result.workers.length > 25) throw new Error("Invalid worker list");
          for (const value of result.workers) {
            if (version !== generation) return;
            await attach(workerFrom(value));
          }
          if (version !== generation) return;
          const next = result.next_offset;
          if (next == null) { offset = 0; break; }
          if (!Number.isSafeInteger(next) || next <= offset || next > 1000000) throw new Error("Invalid worker page");
          offset = next;
        }
      } catch {
        if (version === generation) publish("Worker observation unavailable or definition changed; no fallback. Restore definition/reload or reattach explicitly.");
      } finally { if (version === generation) syncing = false; }
    },
    async reattach(requestId: string) {
      if (!context || !requestId || requestId.length > 200) throw new Error("Supply one worker request ID");
      const version = generation;
      const result = await request("worker_view", { request_id: requestId });
      if (version !== generation) throw new Error("Observer session changed");
      discovery = undefined; // Explicit retry also retries plugin discovery.
      const id = await attach(workerFrom(result.worker, requestId), true);
      return { observation_only: true, native_attachment_confirmed: Boolean(id), observer_id: id ?? null, status };
    },
  };
}
