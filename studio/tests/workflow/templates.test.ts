/** Server-backed workflow catalog helpers and n8n detection. */

import { describe, expect, it } from 'vitest';

import type {
  WorkflowScaffoldResponse,
  WorkflowTemplateDetail,
  WorkflowTemplateSummary,
} from '../../src/api/types';
import {
  BLANK_WORKFLOW_TEMPLATE,
  SCAFFOLD_DATA_FLOW_NOTICE,
  TEMPLATE_PROVENANCE_NOTICE,
  detailToCreatableTemplate,
  filterWorkflowTemplates,
  normalizeWorkflowTemplateSummaries,
  safeHttpsUrl,
  scaffoldToCreatableTemplate,
  sortWorkflowTemplates,
  templateCompatibilityLabel,
  templateRequirementLabels,
  templateSourceView,
  workflowTemplateCategories,
  workflowTemplatePlatforms,
} from '../../src/pages/workflows/model/templates';
import { parseWorkflowYaml } from '../../src/workflow/convert';
import { detectN8nWorkflow } from '../../src/workflow/n8n';

const WORKFLOW_OBJECT: Record<string, unknown> = {
  app: { mode: 'workflow', name: 'Structured catalog workflow' },
  kind: 'app',
  version: '0.6.0',
  workflow: {
    graph: {
      nodes: [
        {
          id: 'start_1',
          data: {
            type: 'start',
            title: 'Start',
            variables: [
              {
                variable: 'query',
                label: 'Query',
                type: 'text-input',
                required: true,
              },
            ],
          },
        },
        {
          id: 'end_1',
          data: {
            type: 'end',
            title: 'End',
            outputs: [{ variable: 'result', value_selector: ['start_1', 'query'] }],
          },
        },
      ],
      edges: [{ source: 'start_1', target: 'end_1', sourceHandle: 'source' }],
    },
  },
};

function summary(
  id: string,
  options: {
    name?: string;
    description?: string;
    categories?: string[];
    provider?: string;
    popularity?: number;
  } = {},
): WorkflowTemplateSummary {
  return {
    id,
    name: options.name ?? id,
    description: options.description ?? '',
    categories: options.categories ?? [],
    tags: [],
    intents: [],
    examples: [],
    compatibility: {
      format: 'dify',
      dsl_version: '0.6.0',
      status: 'supported',
    },
    requirements: [{ kind: 'model', name: 'llm', required: true }],
    source: {
      provider: options.provider ?? 'dify',
      title: `${id} source`,
      author: 'Example author',
      upstream_id: id,
      upstream_url: `https://example.com/${id}`,
      license_status: 'verified',
      observed_metric: 'likes',
      observed_value: options.popularity ?? 0,
      observed_at: '2026-07-14',
    },
  };
}

describe('workflow catalog helpers', () => {
  it('keeps Blank as a parseable local Dify 0.6.0 fallback', () => {
    const workflow = parseWorkflowYaml(BLANK_WORKFLOW_TEMPLATE.yaml);
    expect(BLANK_WORKFLOW_TEMPLATE.id).toBe('blank');
    expect(workflow.version).toBe('0.6.0');
    expect(workflow.nodes.map((node) => node.type)).toEqual(['start', 'llm', 'end']);
  });

  it('uses an unambiguous authorship and redistribution notice', () => {
    expect(TEMPLATE_PROVENANCE_NOTICE).toBe(
      'Spine-authored equivalent · upstream reference only · config not redistributed',
    );
  });

  it('discloses that scaffold descriptions are sent to the configured server', () => {
    expect(SCAFFOLD_DATA_FLOW_NOTICE).toBe(
      'Studio sends this description to the configured RAGSpine server, which returns a Dify-compatible definition. Studio saves it locally and does not execute it.',
    );
  });

  it('normalizes untrusted list fields and drops entries without stable identity', () => {
    const templates = normalizeWorkflowTemplateSummaries([
      null,
      { id: '', name: 'missing id' },
      {
        id: 'paper-rag',
        name: 'Paper RAG',
        description: 42,
        categories: ['Research', 1, null],
        tags: 'not-a-list',
        compatibility: { format: 'dify', dsl_version: 6, status: 'supported' },
        requirements: [null, { kind: 'model', name: 'llm', required: true }, { kind: 'bad' }],
        source: {
          provider: 'n8n',
          upstream_url: 'javascript:alert(1)',
          observed_value: 'many',
        },
      },
    ]);

    expect(templates).toHaveLength(1);
    expect(templates[0]?.categories).toEqual(['Research']);
    expect(templates[0]?.tags).toEqual([]);
    expect(templates[0]?.compatibility).toEqual({
      format: 'dify',
      dsl_version: '',
      status: 'supported',
    });
    expect(templates[0]?.requirements).toEqual([{ kind: 'model', name: 'llm', required: true }]);
    expect(templateSourceView(templates[0]!).httpsUrl).toBeNull();
  });

  it('searches semantics and filters category/platform before popularity sorting', () => {
    const templates = [
      summary('forms', {
        name: 'Form understanding',
        description: 'Extract tables from CNN research papers',
        categories: ['Research'],
        provider: 'dify',
        popularity: 12,
      }),
      summary('support', {
        name: 'Support bot',
        categories: ['Support'],
        provider: 'n8n',
        popularity: 200,
      }),
      summary('papers', {
        name: 'Paper review',
        categories: ['Research'],
        provider: 'dify',
        popularity: 99,
      }),
    ];

    expect(
      filterWorkflowTemplates(templates, {
        query: 'paper',
        category: 'Research',
        platform: 'dify',
      }).map((template) => template.id),
    ).toEqual(['papers', 'forms']);
    expect(workflowTemplateCategories(templates)).toEqual(['Research', 'Support']);
    expect(workflowTemplatePlatforms(templates)).toEqual(['dify', 'n8n']);
  });

  it('sorts observed popularity descending and puts missing values last', () => {
    const missing = { ...summary('missing'), source: null };
    expect(
      sortWorkflowTemplates([
        missing,
        summary('low', { popularity: 1 }),
        summary('high', { popularity: 9 }),
      ]).map((template) => template.id),
    ).toEqual(['high', 'low', 'missing']);
  });

  it('formats compatibility, requirements, and only permits HTTPS provenance links', () => {
    const template = summary('safe');
    expect(templateCompatibilityLabel(template)).toBe('dify · 0.6.0 · supported');
    expect(templateRequirementLabels(template)).toEqual(['model: llm']);
    expect(safeHttpsUrl('https://example.com/path')).toBe('https://example.com/path');
    expect(safeHttpsUrl('http://example.com/path')).toBeNull();
    expect(safeHttpsUrl('javascript:alert(1)')).toBeNull();
  });

  it('prefers canonical workflow JSON over legacy YAML for template details', () => {
    const detail: WorkflowTemplateDetail = {
      ...summary('structured', { name: 'Catalog display name' }),
      request_id: 'req-1',
      workflow: WORKFLOW_OBJECT,
      yaml: 'not: [valid',
    };
    const creatable = detailToCreatableTemplate(detail);
    expect(creatable.name).toBe('Catalog display name');
    expect(parseWorkflowYaml(creatable.yaml).name).toBe('Structured catalog workflow');
  });

  it('turns a structured scaffold response into a createFromTemplate-compatible document', () => {
    const response: WorkflowScaffoldResponse = {
      request_id: 'req-2',
      workflow: WORKFLOW_OBJECT,
      origin: 'generated',
      template_id: null,
      confidence: 0.72,
      matcher: 'lexical',
      warnings: [],
      compatibility: {
        format: 'dify',
        dsl_version: '0.6.0',
        status: 'supported',
      },
      requirements: [],
      source: null,
    };
    const creatable = scaffoldToCreatableTemplate(response, 'A paper RAG workflow');
    expect(creatable.name).toBe('Structured catalog workflow');
    expect(parseWorkflowYaml(creatable.yaml).version).toBe('0.6.0');
  });

  it('accepts legacy YAML only when canonical workflow JSON is absent', () => {
    const response: WorkflowScaffoldResponse = {
      request_id: 'req-3',
      yaml: BLANK_WORKFLOW_TEMPLATE.yaml,
      origin: 'template',
      template_id: 'blankish',
      confidence: 1,
      matcher: 'lexical',
      warnings: [],
      compatibility: {
        format: 'dify',
        dsl_version: '0.6.0',
        status: 'supported',
      },
      requirements: [],
      source: null,
    };
    expect(parseWorkflowYaml(scaffoldToCreatableTemplate(response, 'fallback').yaml).version).toBe(
      '0.6.0',
    );
  });
});

describe('n8n workflow detection', () => {
  const n8nJson = JSON.stringify({
    name: 'My n8n workflow',
    nodes: [
      {
        id: '1',
        name: 'Start',
        type: 'n8n-nodes-base.manualTrigger',
        parameters: {},
      },
    ],
    connections: {},
  });

  it('accepts an n8n JSON export', () => {
    expect(detectN8nWorkflow(n8nJson)).not.toBeNull();
  });

  it('rejects Dify JSON/YAML and garbage', () => {
    expect(detectN8nWorkflow(JSON.stringify(WORKFLOW_OBJECT))).toBeNull();
    expect(detectN8nWorkflow(BLANK_WORKFLOW_TEMPLATE.yaml)).toBeNull();
    expect(detectN8nWorkflow('not json {{{')).toBeNull();
    expect(detectN8nWorkflow(`{"nodes":[],"connections":{},"padding":"${'x'.repeat(1024 * 1024)}"}`)).toBeNull();
  });
});
