/** React Flow adapters: toFlow/fromFlow round trip and container handling. */

import { describe, expect, it } from 'vitest';

import { parseWorkflowYaml } from '../../src/workflow/convert';
import { DIFY_ITERATION, DIFY_NODE, fromFlow, toFlow } from '../../src/workflow/reactflow';
import type { StudioWorkflow } from '../../src/workflow/types';
import { FIXTURE_NAMES, loadFixtureText } from './helpers';

function sortById<T extends { id: string }>(items: T[]): T[] {
  return [...items].sort((a, b) => a.id.localeCompare(b.id));
}

function expectSemanticallyEqual(actual: StudioWorkflow, expected: StudioWorkflow): void {
  expect(actual.name).toBe(expected.name);
  expect(actual.mode).toBe(expected.mode);
  expect(actual.version).toBe(expected.version);
  expect(actual.appPassthrough).toEqual(expected.appPassthrough);
  expect(actual.docPassthrough).toEqual(expected.docPassthrough);
  expect(actual.workflowPassthrough).toEqual(expected.workflowPassthrough);
  expect(actual.graphPassthrough).toEqual(expected.graphPassthrough);
  expect(sortById(actual.nodes)).toEqual(sortById(expected.nodes));
  expect(sortById(actual.edges)).toEqual(sortById(expected.edges));
}

for (const name of FIXTURE_NAMES) {
  it(`toFlow -> fromFlow round trip is lossless: ${name}`, () => {
    const wf = parseWorkflowYaml(loadFixtureText(name));
    const { nodes, edges } = toFlow(wf);
    expectSemanticallyEqual(fromFlow(nodes, edges, wf), wf);
  });
}

describe('toFlow', () => {
  const wf = parseWorkflowYaml(loadFixtureText('iteration'));
  const { nodes, edges } = toFlow(wf);

  it('emits children after their parent (React Flow requirement)', () => {
    const ids = nodes.map((n) => n.id);
    expect(ids.indexOf('iter_llm')).toBeGreaterThan(ids.indexOf('iter_1'));
  });

  it('maps containers to DIFY_ITERATION with style dimensions', () => {
    const container = nodes.find((n) => n.id === 'iter_1');
    expect(container?.type).toBe(DIFY_ITERATION);
    expect(typeof container?.style?.width).toBe('number');
    expect(typeof container?.style?.height).toBe('number');
  });

  it('gives children parentId and extent parent', () => {
    const child = nodes.find((n) => n.id === 'iter_llm');
    expect(child?.type).toBe(DIFY_NODE);
    expect(child?.parentId).toBe('iter_1');
    expect(child?.extent).toBe('parent');
  });

  it('carries the full dify data and passthrough in node data', () => {
    for (const rf of nodes) {
      const source = wf.nodes.find((n) => n.id === rf.id);
      expect(rf.data.dify).toEqual(source?.data);
      expect(rf.data.passthrough).toEqual(source?.passthrough);
    }
  });

  it('carries edge passthrough in edge.data', () => {
    for (const rf of edges) {
      const source = wf.edges.find((e) => e.id === rf.id);
      expect(rf.data?.passthrough).toEqual(source?.passthrough);
      expect(rf.sourceHandle).toBe(source?.sourceHandle);
      expect(rf.targetHandle).toBe(source?.targetHandle);
    }
  });
});

describe('fromFlow', () => {
  it('syncs parentId into data.iteration_id when a node is dropped into a container', () => {
    const wf = parseWorkflowYaml(loadFixtureText('iteration'));
    const { nodes, edges } = toFlow(wf);
    const moved = nodes.map((n) =>
      n.id === 'end_1' ? { ...n, parentId: 'iter_1', extent: 'parent' as const } : n,
    );
    const rebuilt = fromFlow(moved, edges, wf);
    const end = rebuilt.nodes.find((n) => n.id === 'end_1');
    expect(end?.parentId).toBe('iter_1');
    expect(end?.data.iteration_id).toBe('iter_1');
  });

  it('drops data.iteration_id when a node is detached from its container', () => {
    const wf = parseWorkflowYaml(loadFixtureText('iteration'));
    const { nodes, edges } = toFlow(wf);
    const detached = nodes.map((n) => {
      if (n.id !== 'iter_llm') return n;
      const { parentId: _parentId, extent: _extent, ...rest } = n;
      return rest;
    });
    const rebuilt = fromFlow(detached, edges, wf);
    const llm = rebuilt.nodes.find((n) => n.id === 'iter_llm');
    expect(llm?.parentId).toBeUndefined();
    expect('iteration_id' in (llm?.data ?? {})).toBe(false);
  });

  it('writes resized container style back into passthrough', () => {
    const wf = parseWorkflowYaml(loadFixtureText('iteration'));
    const { nodes, edges } = toFlow(wf);
    const resized = nodes.map((n) =>
      n.id === 'iter_1' ? { ...n, style: { width: 999, height: 555 } } : n,
    );
    const rebuilt = fromFlow(resized, edges, wf);
    const container = rebuilt.nodes.find((n) => n.id === 'iter_1');
    expect(container?.passthrough.width).toBe(999);
    expect(container?.passthrough.height).toBe(555);
  });
});
