// Owned one-task Pi SDK bridge. No resource discovery and no shell tools.
import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const VERSION = '0.99.2';
const OUTPUT_LIMIT = 4 * 1024 * 1024;
const PROMPT_LIMIT = 1024 * 1024;

class BridgeError extends Error {}

function assert(condition, message) {
  if (!condition) throw new BridgeError(message);
}

function noCommands(value) {
  if (typeof value === 'string') assert(!value.trimStart().startsWith('!'), 'Command credentials are forbidden');
  else if (value && typeof value === 'object') for (const item of Object.values(value)) noCommands(item);
}

async function promptInput(options) {
  if (options.prompt !== undefined) return options.prompt;
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    assert(size <= PROMPT_LIMIT, 'Prompt exceeds limit');
    chunks.push(chunk);
  }
  return new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks));
}

function brokerClient(options) {
  const child = spawn(options.python, ['-I', options.broker, 'serve', JSON.stringify({
    cwd: options.cwd, project_root: options.project_root, mode: options.mode,
  })], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: '/usr/bin:/bin', LANG: 'C.UTF-8' } });
  let pending;
  let buffer = Buffer.alloc(0);
  let chain = Promise.resolve();
  let dead = false;
  const fail = () => {
    dead = true;
    pending?.reject(new Error('File broker failed'));
    pending = undefined;
  };
  child.on('error', fail);
  child.on('exit', fail);
  child.stdin.on('error', fail);
  child.stderr.on('data', () => {});
  child.stdout.on('data', chunk => {
    buffer = Buffer.concat([buffer, chunk]);
    if (buffer.length > 2 * PROMPT_LIMIT) { fail(); child.kill(); return; }
    for (;;) {
      const end = buffer.indexOf(10);
      if (end === -1) break;
      const line = buffer.subarray(0, end);
      buffer = buffer.subarray(end + 1);
      try {
        assert(pending, 'Unsolicited broker result');
        const result = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(line));
        const current = pending;
        pending = undefined;
        if (result.ok !== true || typeof result.text !== 'string') current.reject(new Error('File tool denied or failed'));
        else current.resolve(result.text);
      } catch { fail(); child.kill(); }
    }
  });
  return {
    execute(name, arguments_) {
      const request = chain.then(() => new Promise((resolve, reject) => {
        if (dead) { reject(new Error('File broker unavailable')); return; }
        const line = JSON.stringify({ name, arguments: arguments_ }) + '\n';
        if (Buffer.byteLength(line) > 2 * PROMPT_LIMIT) { reject(new Error('Tool request exceeds limit')); return; }
        pending = { resolve, reject };
        child.stdin.write(line, error => { if (error) fail(); });
      }));
      chain = request.catch(() => {});
      return request;
    },
    async close() {
      if (child.exitCode !== null || child.signalCode !== null) return;
      const exited = new Promise(resolve => child.once('exit', resolve));
      child.stdin.end();
      const timer = setTimeout(() => child.kill('SIGKILL'), 1000);
      try { await exited; } finally { clearTimeout(timer); }
    },
  };
}

// modelsPath is an in-process test seam, deliberately not a CLI/env option.
// Production never reads user/project models.json or catalog cache files.
export async function runBridge(options, { modelsPath = null } = {}) {
  let session;
  let broker;
  let outputBytes = 0;
  let failed = false;
  let settled = false;
  let finalAssistant;
  const emit = event => {
    const line = JSON.stringify(event) + '\n';
    outputBytes += Buffer.byteLength(line);
    if (outputBytes > OUTPUT_LIMIT) { failed = true; void session?.abort(); return; }
    process.stdout.write(line);
  };
  const policyFailure = message => {
    failed = true;
    emit({ type: 'orchestrator_pi_error', error: message });
    void session?.abort();
  };
  try {
    const metadata = JSON.parse(await readFile(path.join(options.package, 'package.json'), 'utf8'));
    assert(metadata.name === '@earendil-works/pi-coding-agent' && metadata.version === VERSION, 'Unsupported Pi SDK version');
    assert(options.mode === 'read' || options.mode === 'write', 'Invalid file-tool mode');
    assert(typeof options.provider === 'string' && !options.provider.toLowerCase().includes('anthropic'), 'Invalid Pi provider');
    assert(typeof options.model === 'string' && !options.model.toLowerCase().includes('claude'), 'Anthropic models require Claude Code');
    const prompt = await promptInput(options);
    assert(typeof prompt === 'string' && prompt.trim() && !prompt.includes('\0') && Buffer.byteLength(prompt) <= PROMPT_LIMIT, 'Invalid prompt');
    const importFile = relative => import(pathToFileURL(path.join(options.package, relative)).href);
    const sdk = await importFile('dist/index.js');
    const { AuthStorage, FileAuthStorageBackend } = await importFile('dist/core/auth-storage.js');
    const { toJsonEvent } = await importFile('dist/modes/json-event.js');
    const require = createRequire(path.join(options.package, 'package.json'));
    const { Type } = await import(pathToFileURL(require.resolve('typebox')).href);
    // Pi 0.99.2's model capability contract; the session verifies it again below.
    const supportedLevels = model => !model.reasoning ? ['off'] :
      ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].filter(level => {
        const mapped = model.thinkingLevelMap?.[level];
        return mapped !== null && (!['xhigh', 'max'].includes(level) || mapped !== undefined);
      });

    // Validate raw values under the same canonical storage lock before AuthStorage
    // can resolve a !command. Preserve its normal locking and OAuth writeback.
    const backend = new FileAuthStorageBackend(options.auth_path);
    const guard = current => { if (current) noCommands(JSON.parse(current)); };
    const store = AuthStorage.fromStorage({
      withLock: fn => backend.withLock(current => { guard(current); return fn(current); }),
      withLockAsync: (fn, operation) => backend.withLockAsync(async current => {
        guard(current);
        return fn(current);
      }, operation),
    });
    const credentials = {
      read: (provider, operation) => provider === options.provider ? store.read(provider, operation) : Promise.resolve(undefined),
      list: async operation => (await store.list(operation)).filter(item => item.providerId === options.provider || item.provider === options.provider),
      modify: (provider, fn, operation) => {
        assert(provider === options.provider, 'Foreign credential access');
        return store.modify(provider, async current => { const next = await fn(current); noCommands(next); return next; }, operation);
      },
      delete: () => { throw new Error('Credential deletion is forbidden'); },
    };
    const modelRuntime = await sdk.ModelRuntime.create({ credentials, modelsPath, allowModelNetwork: false });
    const model = modelRuntime.getModel(options.provider, options.model);
    assert(model && model.provider === options.provider && model.id === options.model, 'Requested Pi model is unavailable in the catalog');
    assert(supportedLevels(model).includes(options.effort), 'Requested effort is unsupported; no downgrade');
    assert((await modelRuntime.getAvailable(options.provider)).some(item => item.id === options.model && item.provider === options.provider), 'Requested Pi model has no usable authentication');
    const allowed = options.tools;
    const permitted = new Set(['read', 'find', 'grep', 'ls', ...(options.mode === 'write' ? ['edit', 'write'] : [])]);
    assert(Array.isArray(allowed) && new Set(allowed).size === allowed.length && allowed.every(name => permitted.has(name)), 'Invalid tool allowlist');
    broker = brokerClient(options);
    const filePath = Type.String({ description: 'Path inside the authorized roots; no hidden or control files.' });
    const schemas = {
      read: Type.Object({ path: filePath, offset: Type.Optional(Type.Integer({ minimum: 1 })), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 2000 })) }),
      ls: Type.Object({ path: Type.Optional(filePath), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 1000 })) }),
      find: Type.Object({ pattern: Type.String(), path: Type.Optional(filePath), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 1000 })) }),
      grep: Type.Object({ pattern: Type.String({ description: 'Literal text, not a regular expression.' }), path: Type.Optional(filePath), ignoreCase: Type.Optional(Type.Boolean()), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 1000 })) }),
      write: Type.Object({ path: filePath, content: Type.String() }),
      edit: Type.Object({ path: filePath, edits: Type.Array(Type.Object({ oldText: Type.String(), newText: Type.String() }), { minItems: 1, maxItems: 100 }) }),
    };
    const customTools = allowed.map(name => ({
      name, label: name, description: `Restricted ${name} file operation. Text files only. No shell execution.`, parameters: schemas[name],
      async execute(_id, arguments_, signal) {
        try {
          assert(!failed && !signal?.aborted, 'Worker has stopped');
          const text = await broker.execute(name, arguments_);
          return { content: [{ type: 'text', text }], details: undefined };
        } catch {
          policyFailure('File tool denied or failed');
          throw new Error('File tool denied or failed');
        }
      },
    }));
    const resourceLoader = {
      getExtensions: () => ({ extensions: [], errors: [], runtime: sdk.createExtensionRuntime() }),
      getSkills: () => ({ skills: [], diagnostics: [] }), getPrompts: () => ({ prompts: [], diagnostics: [] }),
      getThemes: () => ({ themes: [], diagnostics: [] }), getAgentsFiles: () => ({ agentsFiles: [] }),
      getSystemPrompt: () => `You are a supervised worker. Follow only the supplied task. Use the declared restricted file tools. Read project files at ${options.project_root || options.cwd}. ${options.mode === 'write' ? `Write only at ${options.cwd}.` : 'Relative tool paths start at the project root.'} Never execute commands or change control files. Return your result as text.`,
      getSystemPromptSource: () => undefined, getAppendSystemPrompt: () => [], getAppendSystemPromptSources: () => [],
      extendResources: () => {}, reload: async () => {},
    };
    const sessionManager = sdk.SessionManager.inMemory(options.cwd);
    ({ session } = await sdk.createAgentSession({
      cwd: options.cwd, agentDir: options.agent_dir, modelRuntime, model, thinkingLevel: options.effort,
      resourceLoader, customTools, tools: allowed, sessionManager,
      settingsManager: sdk.SettingsManager.inMemory({ defaultProjectTrust: 'never', cacheWarming: 'off', compaction: { enabled: false }, retry: { enabled: false, provider: { maxRetries: 0 } }, enableInstallTelemetry: false }),
    }));
    assert(session.thinkingLevel === options.effort, 'Pi changed the requested effort');
    assert(JSON.stringify([...session.getActiveToolNames()].sort()) === JSON.stringify([...allowed].sort()), 'Pi changed the tool allowlist');
    emit({ type: 'orchestrator_pi_preflight', version: VERSION, provider: model.provider, model: model.id, effort: options.effort, mode: options.mode, tools: allowed, verified: true });
    emit(sessionManager.getHeader());
    session.subscribe(event => {
      if (settled) { policyFailure('Activity after settlement'); return; }
      if (event.type === 'message_end' && event.message.role === 'assistant') {
        finalAssistant = event.message;
        if (event.message.provider !== model.provider || event.message.model !== model.id || ['error', 'aborted', 'length', 'deferred'].includes(event.message.stopReason)) failed = true;
        for (const block of event.message.content) {
          if (block.type === 'toolCall' && !allowed.includes(block.name)) policyFailure('Forbidden tool requested');
        }
      }
      if (event.type === 'tool_execution_start' && !allowed.includes(event.toolName)) policyFailure('Forbidden tool execution');
      if (event.type === 'tool_execution_end' && event.isError) failed = true;
      if (event.type === 'agent_settled') settled = true;
      emit(toJsonEvent(event));
    });
    const disposition = await session.prompt(prompt, { expandPromptTemplates: false });
    void disposition;
    assert(!failed && settled && finalAssistant?.stopReason === 'stop', 'Pi did not complete successfully');
    assert(finalAssistant.content.some(block => block.type === 'text' && block.text.trim()), 'Missing final assistant text');
    return 0;
  } catch (error) {
    // Error strings from providers may include credentials or content. Keep stdout generic.
    policyFailure('Pi preflight or execution failed');
    if (!session) process.stderr.write(`Pi preflight: ${error instanceof BridgeError ? error.message : 'initialization failed'}\n`);
    return 1;
  } finally {
    session?.dispose();
    await broker?.close();
  }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  try {
    const options = JSON.parse(process.argv[2]);
    process.exitCode = await runBridge(options);
  } catch {
    process.stderr.write('Invalid Pi bridge invocation\n');
    process.exitCode = 1;
  }
}
