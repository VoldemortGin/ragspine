/** Auto-layout: position filling, container-relative children, immutability. */

import { describe, expect, it } from 'vitest';

import { parseWorkflowYaml } from '../../src/workflow/convert';
import {
  autoLayoutWorkflow,
  CONTAINER_HEADER_HEIGHT,
  DEFAULT_NODE_HEIGHT,
  DEFAULT_NODE_WIDTH,
} from '../../src/workflow/layout';
import type { StudioWorkflow } from '../../src/workflow/types';
import { loadFixtureText } from './helpers';

function positionOf(wf: StudioWorkflow, id: string) {
  const node = wf.nodes.find((n) => n.id === id);
  if (node === undefined) throw new Error(`node "${id}" not found`);
  return node.position;
}

describe('auto-layout on import (fixtures carry no positions)', () => {
  it('fills every node with a finite position', () => {
    const wf = parseWorkflowYaml(loadFixtureText('seq'));
    for (const node of wf.nodes) {
      expect(Number.isFinite(node.position.x)).toBe(true);
      expect(Number.isFinite(node.position.y)).toBe(true);
    }
  });

  it('lays the seq chain out left-to-right', () => {
    const wf = parseWorkflowYaml(loadFixtureText('seq'));
    expect(positionOf(wf, 'start_1').x).toBeLessThan(positionOf(wf, 'llm_1').x);
    expect(positionOf(wf, 'llm_1').x).toBeLessThan(positionOf(wf, 'tt_1').x);
    expect(positionOf(wf, 'tt_1').x).toBeLessThan(positionOf(wf, 'end_1').x);
  });

  it('separates parallel siblings and keeps all positions distinct', () => {
    const wf = parseWorkflowYaml(loadFixtureText('parallel'));
    const a = positionOf(wf, 'llm_a');
    const b = positionOf(wf, 'llm_b');
    expect(a.x === b.x && a.y === b.y).toBe(false);
    const keys = new Set(wf.nodes.map((n) => `${n.position.x},${n.position.y}`));
    expect(keys.size).toBe(wf.nodes.length);
  });
});

describe('iteration containers', () => {
  const wf = parseWorkflowYaml(loadFixtureText('iteration'));
  const container = wf.nodes.find((n) => n.id === 'iter_1');
  const child = wf.nodes.find((n) => n.id === 'iter_llm');

  it('sizes the container and stores it in passthrough', () => {
    expect(typeof container?.passthrough.width).toBe('number');
    expect(typeof container?.passthrough.height).toBe('number');
  });

  it('positions children relative to the container, below the header', () => {
    const position = child?.position;
    const width = container?.passthrough.width as number;
    const height = container?.passthrough.height as number;
    expect(position).toBeDefined();
    if (position === undefined) return;
    expect(position.x).toBeGreaterThanOrEqual(0);
    expect(position.y).toBeGreaterThanOrEqual(CONTAINER_HEADER_HEIGHT);
    expect(position.x + DEFAULT_NODE_WIDTH).toBeLessThanOrEqual(width);
    expect(position.y + DEFAULT_NODE_HEIGHT).toBeLessThanOrEqual(height);
  });
});

describe('autoLayoutWorkflow', () => {
  it('keeps existing positions without force', () => {
    const wf = parseWorkflowYaml(loadFixtureText('seq'));
    const moved: StudioWorkflow = {
      ...wf,
      nodes: wf.nodes.map((n) => (n.id === 'llm_1' ? { ...n, position: { x: 7777, y: 8888 } } : n)),
    };
    const relaid = autoLayoutWorkflow(moved);
    expect(positionOf(relaid, 'llm_1')).toEqual({ x: 7777, y: 8888 });
  });

  it('recomputes all positions with force', () => {
    const wf = parseWorkflowYaml(loadFixtureText('seq'));
    const moved: StudioWorkflow = {
      ...wf,
      nodes: wf.nodes.map((n) => (n.id === 'llm_1' ? { ...n, position: { x: 7777, y: 8888 } } : n)),
    };
    const relaid = autoLayoutWorkflow(moved, { force: true });
    expect(positionOf(relaid, 'llm_1')).not.toEqual({ x: 7777, y: 8888 });
    expect(positionOf(relaid, 'start_1').x).toBeLessThan(positionOf(relaid, 'llm_1').x);
  });

  it('does not mutate the input workflow', () => {
    const wf = parseWorkflowYaml(loadFixtureText('iteration'));
    const snapshot = JSON.stringify(wf);
    const result = autoLayoutWorkflow(wf, { force: true });
    expect(result).not.toBe(wf);
    expect(JSON.stringify(wf)).toBe(snapshot);
  });

  it('never touches node data objects', () => {
    const wf = parseWorkflowYaml(loadFixtureText('iteration'));
    const result = autoLayoutWorkflow(wf, { force: true });
    for (const node of result.nodes) {
      const original = wf.nodes.find((n) => n.id === node.id);
      expect(node.data).toBe(original?.data);
    }
  });
});
