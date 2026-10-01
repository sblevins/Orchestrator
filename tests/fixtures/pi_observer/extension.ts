import { createWorkerObservers } from "PRODUCTION_OBSERVER_MODULE";
import { readFileSync, appendFileSync } from "node:fs";
const root = process.env.OBSERVER_FIXTURE!;
export default function (pi: any) {
  let id: string | undefined;
  const log = (event: string, data?: unknown) => appendFileSync(root + "/events.jsonl", JSON.stringify({event, data}) + "\n");
  const observers = createWorkerObservers(pi, process.cwd(), async (action, payload) => {
    if (action !== "worker_view") throw new Error("Unexpected worker mutation");
    const worker = JSON.parse(readFileSync(root + "/worker.json", "utf8"));
    if (payload?.request_id) {
      if (payload.request_id !== worker.request_id) throw new Error("Wrong worker identity");
      log("worker-detail-read");
      return {worker};
    }
    return {workers: [worker], next_offset: null};
  });
  for (const event of ["subagents:started", "subagents:completed", "subagents:failed"]) {
    pi.events.on(event, (data: any) => { if (event === "subagents:started") id = data.id; log(event, data); });
  }
  pi.on("agent_start", () => log("parent-agent-start"));
  pi.on("session_start", (_event: unknown, ctx: any) => observers.start(ctx));
  pi.on("session_shutdown", () => observers.stop());
  pi.registerCommand("fixture-sync", {handler: async () => { await observers.sync(); }});
  pi.registerCommand("fixture-attach", {handler: async () => {
    try { log("attached", await observers.reattach("fixture-worker")); }
    catch { log("fixture-error"); }
  }});
  pi.registerCommand("fixture-detach", {handler: async () => {
    pi.events.emit("subagents:rpc:stop", {requestId: crypto.randomUUID(), agentId: id});
  }});
  pi.registerCommand("fixture-quit", {handler: async (_args: unknown, ctx: any) => ctx.shutdown()});
}
