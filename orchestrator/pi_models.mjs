// Exact pins win; family selection uses the configured provider's complete local catalog.
export function isPiFamily(value) {
  return typeof value === 'string' && /^(astra|sol)$/i.test(value);
}

function rank(id, family) {
  const match = id.match(new RegExp(`^(?:openai/)?gpt-(\\d+(?:[.-]\\d+)*)-${family}(?:-(\\d{4}-\\d{2}-\\d{2}|\\d{8}))?$`));
  if (!match) return undefined; // Never substitute pro, fast, preview, batch or rolling aliases.
  const parts = match[1].split(/[.-]/).map(part => BigInt(part));
  let date = 0;
  if (match[2]) {
    const stamp = match[2].replaceAll('-', '');
    const year = Number(stamp.slice(0, 4));
    const month = Number(stamp.slice(4, 6));
    const day = Number(stamp.slice(6, 8));
    const parsed = new Date(`${stamp.slice(0, 4)}-${stamp.slice(4, 6)}-${stamp.slice(6, 8)}T00:00:00Z`);
    if (!year || parsed.getUTCFullYear() !== year || parsed.getUTCMonth() + 1 !== month || parsed.getUTCDate() !== day) return undefined;
    date = Number(stamp);
  }
  return { parts, date };
}

function compare(left, right) {
  for (let index = 0; index < Math.max(left.parts.length, right.parts.length); index++) {
    const a = left.parts[index] ?? 0n;
    const b = right.parts[index] ?? 0n;
    if (a !== b) return a > b ? 1 : -1;
  }
  return Math.sign(left.date - right.date);
}

export function resolveWorkerModel(runtime, provider, requested) {
  const exact = runtime.getModel(provider, requested);
  if (exact) return exact;
  if (!isPiFamily(requested)) throw new Error(`Configured model is unavailable: ${provider}/${requested}`);
  let best;
  let ambiguous = false;
  for (const model of runtime.getModels(provider)) {
    if (model.provider !== provider) continue;
    const candidate = rank(model.id, requested.toLowerCase());
    if (!candidate) continue;
    const ordering = best ? compare(candidate, best.rank) : 1;
    if (ordering > 0) { best = { model, rank: candidate }; ambiguous = false; }
    else if (ordering === 0) ambiguous = true;
  }
  if (ambiguous) throw new Error(`Ambiguous model family ${provider}/${requested}; configure an exact ID`);
  if (!best) throw new Error(`No stable base model for ${provider}/${requested} in this installed catalog; configure an exact ID or update the catalog`);
  return best.model;
}

export function resolveWorkerEffort(model, requested, supportedLevels) {
  const supported = supportedLevels(model);
  const resolved = requested === 'max-supported' ? supported.at(-1) : requested;
  if (!supported.length || !supported.includes(resolved)) throw new Error('Requested effort is unsupported by the selected model; no fallback');
  return { resolved, supported };
}
