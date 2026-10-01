import type { Api, Model } from "@earendil-works/pi-ai";

/** Only these bare names opt into family selection. Exact IDs are never normalized. */
export function isAnthropicFamily(requested: string): boolean {
  return /^(opus|sonnet|haiku|fable)$/i.test(requested);
}

type CatalogRegistry = {
  find(provider: string, modelId: string): Model<Api> | undefined;
  getAll(): Model<Api>[];
};
type VersionRank = { version: bigint[]; date: number };

function stableVersion(modelId: string, family: string): VersionRank | undefined {
  // The installed catalog uses both claude-opus-4-5 and claude-3-7-sonnet.
  const current = modelId.match(new RegExp(`^claude-${family}-(\\d+(?:-\\d+)*)$`));
  const historical = modelId.match(new RegExp(`^claude-(\\d+(?:-\\d+)*)-${family}(?:-(\\d{8}))?$`));
  if (!current && !historical) return undefined;
  const segments = (current?.[1] ?? historical![1]).split("-");
  const dateText = current && /^\d{8}$/.test(segments[segments.length - 1])
    ? segments.pop() : historical?.[2];
  if (!segments.length) return undefined;
  let date = 0;
  if (dateText) {
    const year = Number(dateText.slice(0, 4));
    const month = Number(dateText.slice(4, 6));
    const day = Number(dateText.slice(6, 8));
    const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    const days = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    if (year === 0 || month < 1 || month > 12 || day < 1 || day > days[month - 1]) return undefined;
    date = Number(dateText);
  }
  return { version: segments.map(segment => BigInt(segment)), date };
}

function compareVersions(left: VersionRank, right: VersionRank): number {
  for (let index = 0; index < Math.max(left.version.length, right.version.length); index++) {
    const leftPart = left.version[index] ?? 0n;
    const rightPart = right.version[index] ?? 0n;
    if (leftPart !== rightPart) return leftPart > rightPart ? 1 : -1;
  }
  return Math.sign(left.date - right.date);
}

/**
 * Select the newest stable version known to this installed catalog, not the newest
 * global release or an entitlement-checked model. Authentication happens afterward;
 * it must not silently downgrade a family to an older version.
 */
export function resolveForegroundModel(registry: CatalogRegistry, provider: string, requested: string): Model<Api> {
  const exact = registry.find(provider, requested);
  if (exact) return exact;
  if (provider === "anthropic" && isAnthropicFamily(requested)) {
    let best: { model: Model<Api>; rank: VersionRank } | undefined;
    let ambiguous = false;
    for (const model of registry.getAll()) {
      if (model.provider !== provider) continue;
      const rank = stableVersion(model.id, requested.toLowerCase());
      if (!rank) continue;
      const comparison = best ? compareVersions(rank, best.rank) : 1;
      if (comparison > 0) {
        best = { model, rank };
        ambiguous = false;
      } else if (comparison === 0) {
        ambiguous = true;
      }
    }
    if (ambiguous) throw new Error(`Ambiguous Orchestrator model family: ${provider}/${requested}; configure an exact model ID`);
    if (best) return best.model;
  }
  throw new Error(`Configured Orchestrator model not found: ${provider}/${requested}`);
}
