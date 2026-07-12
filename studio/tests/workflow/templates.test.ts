/**
 * Template gallery + n8n detection: every built-in template must parse with
 * the studio's own converter (positions auto-laid-out), and n8n JSON
 * detection must accept n8n exports while rejecting Dify YAML and garbage.
 */

import { describe, expect, it } from 'vitest';

import { WORKFLOW_TEMPLATES, templateNodeTypes } from '../../src/pages/workflows/model/templates';
import { parseWorkflowYaml } from '../../src/workflow/convert';
import { detectN8nWorkflow } from '../../src/workflow/n8n';

describe('workflow templates', () => {
  it('has unique ids', () => {
    const ids = WORKFLOW_TEMPLATES.map((t) => t.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('lists Blank first', () => {
    expect(WORKFLOW_TEMPLATES[0]?.id).toBe('blank');
    expect(WORKFLOW_TEMPLATES[0]?.name).toBe('Blank');
  });

  for (const template of WORKFLOW_TEMPLATES) {
    it(`template "${template.id}" parses with nodes, edges and finite positions`, () => {
      const wf = parseWorkflowYaml(template.yaml);
      expect(wf.nodes.length).toBeGreaterThan(0);
      expect(wf.edges.length).toBeGreaterThan(0);
      for (const node of wf.nodes) {
        expect(Number.isFinite(node.position.x)).toBe(true);
        expect(Number.isFinite(node.position.y)).toBe(true);
      }
    });

    it(`template "${template.id}" exposes node-type badges`, () => {
      const types = templateNodeTypes(template.yaml);
      expect(types.length).toBeGreaterThan(0);
      expect(types).toContain('start');
    });
  }

  it('extracts the representative node types', () => {
    const byId = new Map(WORKFLOW_TEMPLATES.map((t) => [t.id, t]));
    expect(templateNodeTypes(byId.get('rag-qa')!.yaml)).toContain('knowledge-retrieval');
    expect(templateNodeTypes(byId.get('branch')!.yaml)).toContain('if-else');
    expect(templateNodeTypes(byId.get('iteration')!.yaml)).toContain('iteration');
    expect(templateNodeTypes(byId.get('parallel')!.yaml)).toContain('template-transform');
  });

  it('does not leak non-node type strings into badges', () => {
    // start variables carry `type: text-input`, which is not a node type
    const rag = WORKFLOW_TEMPLATES.find((t) => t.id === 'rag-qa')!;
    expect(templateNodeTypes(rag.yaml)).not.toContain('text-input');
  });
});

describe('n8n workflow detection', () => {
  const n8nJson = JSON.stringify({
    name: 'My n8n workflow',
    nodes: [
      { id: '1', name: 'Start', type: 'n8n-nodes-base.manualTrigger', parameters: {} },
      { id: '2', name: 'Set', type: 'n8n-nodes-base.set', parameters: {} },
    ],
    connections: { Start: { main: [[{ node: 'Set', type: 'main', index: 0 }]] } },
  });

  it('accepts an n8n JSON export', () => {
    const doc = detectN8nWorkflow(n8nJson);
    expect(doc).not.toBeNull();
    expect(Array.isArray(doc?.['nodes'])).toBe(true);
  });

  it('accepts an empty-but-valid n8n shape', () => {
    expect(detectN8nWorkflow('{"nodes": [], "connections": {}}')).not.toBeNull();
  });

  it('rejects Dify workflow YAML (all templates)', () => {
    for (const template of WORKFLOW_TEMPLATES) {
      expect(detectN8nWorkflow(template.yaml)).toBeNull();
    }
  });

  it('rejects JSON without the n8n shape', () => {
    expect(detectN8nWorkflow('{"nodes": "nope", "connections": {}}')).toBeNull();
    expect(detectN8nWorkflow('{"nodes": []}')).toBeNull();
    expect(detectN8nWorkflow('{"connections": {}}')).toBeNull();
    expect(detectN8nWorkflow('{"nodes": [], "connections": []}')).toBeNull();
    expect(detectN8nWorkflow('[1, 2, 3]')).toBeNull();
    expect(detectN8nWorkflow('42')).toBeNull();
    expect(detectN8nWorkflow('null')).toBeNull();
  });

  it('rejects garbage input', () => {
    expect(detectN8nWorkflow('')).toBeNull();
    expect(detectN8nWorkflow('not json {{{')).toBeNull();
  });
});
