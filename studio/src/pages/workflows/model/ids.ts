/** Unique id generation for canvas nodes/edges. Pure module. */

/** Sanitized id prefix for a node type, e.g. "if-else" -> "if_else". */
export function nodeIdPrefix(type: string): string {
  const cleaned = type
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
  return cleaned === '' ? 'node' : cleaned;
}

/** Type-prefixed counter id ("llm_2") avoiding collisions with existing ids. */
export function uniqueNodeId(type: string, existing: Iterable<string>): string {
  const used = new Set(existing);
  const prefix = nodeIdPrefix(type);
  for (let n = 1; ; n += 1) {
    const id = `${prefix}_${n}`;
    if (!used.has(id)) return id;
  }
}

/** Edge id in the same style convert.ts generates, collision-safe. */
export function uniqueEdgeId(
  source: string,
  sourceHandle: string,
  target: string,
  existing: Iterable<string>,
): string {
  const used = new Set(existing);
  const base = `${source}__${sourceHandle}__${target}`;
  let id = base;
  for (let n = 2; used.has(id); n += 1) id = `${base}__${n}`;
  return id;
}
