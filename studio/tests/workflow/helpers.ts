/** Shared test helpers: fixture loading, raw doc navigation, deep-subset assertion. */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { load } from 'js-yaml';

export const FIXTURE_NAMES = [
  'seq',
  'branch',
  'parallel',
  'iteration',
  'knowledge',
  'agent_tool',
  'qa_fold',
] as const;

export type FixtureName = (typeof FIXTURE_NAMES)[number];

const HERE = path.dirname(fileURLToPath(import.meta.url));
/** studio/tests/workflow -> ragspine/tests/dify/fixtures */
export const FIXTURE_DIR = path.resolve(HERE, '..', '..', '..', 'tests', 'dify', 'fixtures');

export function loadFixtureText(name: FixtureName): string {
  return readFileSync(path.join(FIXTURE_DIR, `${name}.yml`), 'utf8');
}

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function asRecord(value: unknown, what: string): Record<string, unknown> {
  if (!isPlainObject(value)) throw new Error(`Expected ${what} to be a mapping.`);
  return value;
}

export function loadDoc(text: string): Record<string, unknown> {
  return asRecord(load(text), 'yaml document root');
}

function rawGraph(doc: Record<string, unknown>): Record<string, unknown> {
  return asRecord(asRecord(doc['workflow'], 'workflow')['graph'], 'workflow.graph');
}

export function rawNodes(doc: Record<string, unknown>): Record<string, unknown>[] {
  const nodes = rawGraph(doc)['nodes'];
  if (!Array.isArray(nodes)) throw new Error('workflow.graph.nodes must be a list.');
  return nodes.map((n, i) => asRecord(n, `node #${i}`));
}

export function rawEdges(doc: Record<string, unknown>): Record<string, unknown>[] {
  const edges = rawGraph(doc)['edges'];
  if (!Array.isArray(edges)) throw new Error('workflow.graph.edges must be a list.');
  return edges.map((e, i) => asRecord(e, `edge #${i}`));
}

/**
 * Returns a human-readable path + diff message for the first place where
 * `subset` is NOT contained in `superset`, or null when every key/value of
 * `subset` is present with an equal value in `superset` (extra keys in
 * `superset` are allowed at any depth; arrays must match element-wise).
 */
export function findSubsetMismatch(subset: unknown, superset: unknown, at = '$'): string | null {
  if (Array.isArray(subset)) {
    if (!Array.isArray(superset)) {
      return `${at}: expected an array, got ${JSON.stringify(superset)}`;
    }
    if (superset.length !== subset.length) {
      return `${at}: array length changed ${subset.length} -> ${superset.length}`;
    }
    for (let i = 0; i < subset.length; i += 1) {
      const mismatch = findSubsetMismatch(subset[i], superset[i], `${at}[${i}]`);
      if (mismatch !== null) return mismatch;
    }
    return null;
  }
  if (isPlainObject(subset)) {
    if (!isPlainObject(superset)) {
      return `${at}: expected a mapping, got ${JSON.stringify(superset)}`;
    }
    for (const key of Object.keys(subset)) {
      if (!(key in superset)) return `${at}.${key}: key lost in exported doc`;
      const mismatch = findSubsetMismatch(subset[key], superset[key], `${at}.${key}`);
      if (mismatch !== null) return mismatch;
    }
    return null;
  }
  if (!Object.is(subset, superset)) {
    return `${at}: value changed ${JSON.stringify(subset)} -> ${JSON.stringify(superset)}`;
  }
  return null;
}

/** Assert original ⊆ exported with a clear diff message on failure. */
export function assertDeepSubset(subset: unknown, superset: unknown): void {
  const mismatch = findSubsetMismatch(subset, superset);
  if (mismatch !== null) {
    throw new Error(`deep-subset violation: ${mismatch}`);
  }
}
