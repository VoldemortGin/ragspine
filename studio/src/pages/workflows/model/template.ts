/** Minimal starter workflow: start (query) -> llm -> end, auto-laid-out. */

import { autoLayoutWorkflow, missingPosition } from '../../../workflow/layout';
import { nodeRegistry } from '../../../workflow/registry';
import type { StudioNode, StudioWorkflow } from '../../../workflow/types';
import type { LibraryEntry } from './library';

export const DIFY_DSL_VERSION = '0.6.0';
export const OPENAI_PLUGIN_UNIQUE_IDENTIFIER =
  'langgenius/openai:0.3.8@592c8252795b5f75807de2d609a03196ed02596b409f7642b4a07548c7ff57ef';

function node(id: string, data: StudioNode['data']): StudioNode {
  return {
    id,
    type: data.type,
    position: missingPosition(),
    data: { desc: '', selected: false, ...data },
    passthrough: {},
  };
}

export function createTemplateWorkflow(name: string): StudioWorkflow {
  const start: StudioNode['data'] = {
    ...nodeRegistry.start.createDefaultData(),
    title: 'Start',
  };
  const llm: StudioNode['data'] = {
    ...nodeRegistry.llm.createDefaultData(),
    title: 'LLM',
    prompt_template: [{ role: 'user', text: '{{#start_1.query#}}' }],
  };
  const end: StudioNode['data'] = {
    ...nodeRegistry.end.createDefaultData(),
    title: 'End',
    outputs: [
      {
        variable: 'result',
        value_type: 'string',
        value_selector: ['llm_1', 'text'],
      },
    ],
  };

  const wf: StudioWorkflow = {
    name,
    mode: 'workflow',
    version: DIFY_DSL_VERSION,
    appPassthrough: {
      description: 'Spine-authored editable Dify workflow.',
      icon: '🧩',
      icon_background: '#E4FBCC',
      use_icon_as_answer_icon: false,
    },
    docPassthrough: {
      dependencies: [
        {
          current_identifier: null,
          type: 'marketplace',
          value: {
            marketplace_plugin_unique_identifier: OPENAI_PLUGIN_UNIQUE_IDENTIFIER,
          },
        },
      ],
    },
    workflowPassthrough: {
      conversation_variables: [],
      environment_variables: [],
      features: {},
    },
    graphPassthrough: { viewport: { x: 0, y: 0, zoom: 0.7 } },
    nodes: [node('start_1', start), node('llm_1', llm), node('end_1', end)],
    edges: [
      {
        id: 'start_1-source-llm_1-target',
        source: 'start_1',
        target: 'llm_1',
        sourceHandle: 'source',
        targetHandle: 'target',
        passthrough: {},
      },
      {
        id: 'llm_1-source-end_1-target',
        source: 'llm_1',
        target: 'end_1',
        sourceHandle: 'source',
        targetHandle: 'target',
        passthrough: {},
      },
    ],
  };
  return autoLayoutWorkflow(wf, { force: true });
}

/** Next free "Untitled workflow N" name. */
export function untitledName(entries: readonly LibraryEntry[]): string {
  const used = new Set(entries.map((e) => e.name));
  for (let n = 1; ; n += 1) {
    const name = `Untitled workflow ${n}`;
    if (!used.has(name)) return name;
  }
}

/** `base`, "base 2", "base 3", ... avoiding collisions. */
export function uniqueName(base: string, entries: readonly LibraryEntry[]): string {
  const used = new Set(entries.map((e) => e.name));
  if (!used.has(base)) return base;
  for (let n = 2; ; n += 1) {
    const candidate = `${base} ${n}`;
    if (!used.has(candidate)) return candidate;
  }
}

/** "Name copy", "Name copy 2", ... avoiding collisions. */
export function copyName(name: string, entries: readonly LibraryEntry[]): string {
  const used = new Set(entries.map((e) => e.name));
  const base = `${name} copy`;
  if (!used.has(base)) return base;
  for (let n = 2; ; n += 1) {
    const candidate = `${base} ${n}`;
    if (!used.has(candidate)) return candidate;
  }
}
