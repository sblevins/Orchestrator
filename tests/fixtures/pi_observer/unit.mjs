import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const require = createRequire(pathToFileURL(process.argv[3]));
const {createJiti} = require('jiti');
const resolver = createJiti(process.argv[3]);
const jiti = createJiti(import.meta.url, {fsCache: false,
  alias: {'@earendil-works/pi-ai': resolver.esmResolve('@earendil-works/pi-ai')}});
const {createWorkerObservers, OBSERVER_PROVIDER, OBSERVER_MODEL, observerText} = await jiti.import(process.argv[2]);
const listeners = new Map();
const spawns = [], stops = [], requests = [];
let provider, nextId = 0, delayedPage, delayedDetail, paginationMode = false, dropSpawnReplies = false;
const expirationCallbacks = [], lateCleanupCallbacks = [], lostStreamResults = [];
const spawnReplyListenerCount = () => [...listeners.entries()]
  .filter(([name]) => name.startsWith('subagents:rpc:spawn:reply:'))
  .reduce((total, [, callbacks]) => total + callbacks.size, 0);
const originalSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (callback, milliseconds, ...arguments_) => {
  if (milliseconds === 60 * 60 * 1000) expirationCallbacks.push(callback);
  if (milliseconds === 30000) lateCleanupCallbacks.push(callback);
  return originalSetTimeout(callback, dropSpawnReplies && milliseconds === 1500 ? 2 : milliseconds, ...arguments_);
};
const model = {provider: OBSERVER_PROVIDER, id: OBSERVER_MODEL, api: 'orchestrator-local-worker-status'};
const api = {
  registerProvider(name, value) {assert.equal(name, OBSERVER_PROVIDER); provider = value;},
  events: {
    on(name, callback) {const group = listeners.get(name) ?? new Set(); group.add(callback); listeners.set(name, group); return () => group.delete(callback);},
    emit(name, data) {
      if (name === 'subagents:rpc:ping') api.events.emit(`${name}:reply:${data.requestId}`, {success:true, data:{version:2}});
      if (name === 'subagents:rpc:spawn') {
        assert.equal(data.options.model, model);
        assert.equal(data.options.isBackground, undefined);
        assert.equal(data.options.isolated, true);
        const id = String(++nextId); spawns.push({...data, id});
        if (!dropSpawnReplies) api.events.emit(`${name}:reply:${data.requestId}`, {success:true, data:{id}});
        else lostStreamResults.push(stream(spawns.at(-1)).result());
      }
      if (name === 'subagents:rpc:stop') stops.push(data.agentId);
      for (const callback of listeners.get(name) ?? []) callback(data);
    }
  },
};
const ctx = {cwd:process.cwd(), hasUI:true, ui:{setStatus() {}}, modelRegistry:{find:()=>model}};
const workers = Array.from({length:12}, (_, index) => ({request_id:`worker-${index}`, task_id:`task-${index}`, state:'queued', task_state:'running', done:false, accepted:false}));
const observers = createWorkerObservers(api, process.cwd(), async(action, payload, signal) => {
  assert.equal(action, 'worker_view'); requests.push(payload);
  if (payload.request_id) {
    if (delayedDetail) return delayedDetail;
    return {worker: workers.find(worker => worker.request_id === payload.request_id)};
  }
  if (delayedPage) return delayedPage;
  if (paginationMode) return {workers:[], next_offset:payload.offset + 25};
  return {workers, next_offset:null};
});
const tick = () => new Promise(resolve => setImmediate(resolve));
function stream(spawn, tools = []) {
  return provider.streamSimple(model, {messages:[{role:'system',content:'observer',toolsAdded:tools,timestamp:0},
    {role:'user',content:[{type:'text',text:spawn.prompt}],timestamp:0}]}, {});
}
try {
  assert.equal(observerText('\x1b[31mred\x07\u202eevil'), 'redevil');
  observers.start(ctx); await tick(); await observers.sync();
  assert.equal(spawns.length, 8, 'bounded active observers');
  const first = spawns[0];
  api.events.emit('subagents:failed', {id:first.id, status:'stopped'});
  await observers.sync();
  assert.equal(spawns.filter(spawn => spawn.prompt === first.prompt).length, 1);
  assert.equal(spawns.length, 9, 'fill vacancy with a different worker, never respawn stopped worker');
  observers.stop(); assert.equal(stops.length, 8);
  // A saved capability cannot observe after generation invalidation.
  const stale = await stream(first).result();
  assert.equal(stale.stopReason, 'error');
  // A stale list response cannot create observers after stop/start.
  let resolvePage;
  delayedPage = new Promise(resolve => {resolvePage=resolve;});
  observers.start(ctx); await tick();
  const sync = observers.sync(); await tick(); observers.stop();
  resolvePage({workers,next_offset:25}); await sync; delayedPage=undefined;
  assert.equal(spawns.length, 9);
  // Native stream cancellation never issues a worker mutation.
  observers.start(ctx); await tick(); await observers.reattach('worker-0');
  const running = stream(spawns.at(-1));
  const runningResult = running.result(); await tick(); observers.stop();
  assert.equal((await runningResult).stopReason, 'aborted');
  assert(workers.every(worker => !worker.done));
  // Detail response completing after reset is discarded too.
  let resolveDetail;
  delayedDetail = new Promise(resolve => {resolveDetail=resolve;});
  observers.start(ctx); await tick();
  const reattach = observers.reattach('worker-0'); await tick(); observers.stop();
  resolveDetail({worker: workers[0]}); await assert.rejects(reattach, /changed/); delayedDetail=undefined;
  // Final safe text, zero tokens/cost, and single-use capability (resume/steer cannot reroute).
  workers[0].done=true; workers[0].report='safe result';
  observers.start(ctx); await tick(); await observers.reattach('worker-0');
  const finishedSpawn=spawns.at(-1);
  const message=await stream(finishedSpawn).result();
  assert.equal(message.stopReason, 'stop'); assert.equal(message.usage.totalTokens, 0);
  assert.equal(message.usage.cost.total, 0);
  assert.match(message.content[0].text, /safe result/);
  assert.equal((await stream(finishedSpawn).result()).stopReason, 'error');
  // Tool declarations or exact-model mismatch fail closed, before reading any worker.
  await observers.reattach('worker-0');
  const beforeTools=requests.length;
  const forbiddenToolsResult=await stream(spawns.at(-1), [{name:'bash',description:'forbidden',parameters:{type:'object'}}]).result();
  assert.equal(forbiddenToolsResult.stopReason, 'error');
  assert.equal(requests.length, beforeTools);
  model.provider='wrong-provider';
  await assert.rejects(observers.reattach('worker-0'), /Exact local observer model/);
  model.provider=OBSERVER_PROVIDER;
  await observers.reattach('worker-0');
  const timedObserver=spawns.at(-1);
  expirationCallbacks.at(-1)();
  assert(stops.includes(timedObserver.id), 'one-hour timeout detaches the native observer');
  observers.stop(); observers.start(ctx); await tick(); paginationMode=true;
  const beforePages=requests.length;
  await observers.sync();
  assert.deepEqual(requests.slice(beforePages).map(request => request.offset), [0,25,50,75]);
  await observers.sync();
  assert.deepEqual(requests.slice(beforePages+4).map(request => request.offset), [100,125,150,175]);
  assert(requests.every(request => Object.keys(request).every(key => ['offset','request_id'].includes(key))));
  // Lost spawn replies must not consume capacity permanently, including across reset.
  observers.stop(); observers.start(ctx); await tick(); paginationMode=false; dropSpawnReplies=true;
  workers[0].done=false;
  const lostReplies=[];
  for (let index=0; index<8; index++) {
    await assert.rejects(observers.reattach('worker-0'), /unavailable or detached/);
    lostReplies.push(spawns.at(-1));
    assert.equal((await lostStreamResults.at(-1)).stopReason, 'aborted', 'timeout aborts an already running local observer');
    assert(spawnReplyListenerCount() <= 8);
  }
  observers.stop(); observers.start(ctx); await tick(); dropSpawnReplies=false;
  const recovered=await observers.reattach('worker-0');
  assert.equal(recovered.native_attachment_confirmed, true, 'eight lost replies must not permanently exhaust observer capacity');
  for (const lost of lostReplies) {
    const beforeStaleRead=requests.length;
    assert.equal((await stream(lost).result()).stopReason, 'error');
    assert.equal(requests.length, beforeStaleRead, 'lost child has no authorized worker-read capability');
    api.events.emit(`subagents:rpc:spawn:reply:${lost.requestId}`, {success:true,data:{id:lost.id}});
    assert(stops.includes(lost.id), 'late returned observer ID is detached best effort');
  }
  assert.equal(spawnReplyListenerCount(), 0, 'late replies remove their drain listeners');
  // Eviction and expiry bound abandoned reply listeners without blocking new attachments.
  observers.stop(); observers.start(ctx); await tick(); dropSpawnReplies=true;
  const abandoned=[];
  for (let index=0; index<16; index++) {
    await assert.rejects(observers.reattach('worker-0'), /unavailable or detached/);
    abandoned.push(spawns.at(-1));
    assert.equal((await lostStreamResults.at(-1)).stopReason, 'aborted');
    assert(spawnReplyListenerCount() <= 8, 'late reply cleanup is bounded independently of active capacity');
  }
  assert.equal(spawnReplyListenerCount(), 8);
  observers.stop(); observers.start(ctx); await tick(); dropSpawnReplies=false;
  assert.equal((await observers.reattach('worker-0')).native_attachment_confirmed, true);
  for (const cleanup of lateCleanupCallbacks) cleanup();
  assert.equal(spawnReplyListenerCount(), 0, '30-second expiry removes all drain listeners');
  for (const lost of abandoned) {
    const beforeStaleRead=requests.length;
    assert.equal((await stream(lost).result()).stopReason, 'error', 'evicted/expired orphan cannot regain worker access');
    assert.equal(requests.length, beforeStaleRead);
  }
  assert(workers.every(worker => !worker.done), 'reply cleanup never changes owned workers');
  console.log('observer lifecycle unit checks passed');
} finally {observers.stop(); globalThis.setTimeout = originalSetTimeout;}
