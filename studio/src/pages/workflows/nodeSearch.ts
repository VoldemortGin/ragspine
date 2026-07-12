/** Shared node-type search: category grouping + query filtering used by the
 * palette sidebar and the quick-add node picker. Pure module. */

import { nodeRegistry } from '../../workflow/registry';
import type { NodeTypeDefinition } from '../../workflow/types';
import { NODE_TYPES } from '../../workflow/types';

export const CATEGORY_LABELS: Record<NodeTypeDefinition['category'], string> = {
  flow: 'Flow',
  model: 'Model',
  logic: 'Logic',
  transform: 'Transform',
  rag: 'RAG',
  tools: 'Tools',
};

export const CATEGORY_ORDER: NodeTypeDefinition['category'][] = [
  'flow',
  'model',
  'logic',
  'transform',
  'rag',
  'tools',
];

export interface NodeGroup {
  category: NodeTypeDefinition['category'];
  defs: NodeTypeDefinition[];
}

/** Registry types matching `query` (label/type/description substring),
 * optionally pre-filtered by `include`, grouped by category. */
export function searchNodeGroups(
  query: string,
  include?: (def: NodeTypeDefinition) => boolean,
): NodeGroup[] {
  const q = query.trim().toLowerCase();
  const defs = NODE_TYPES.map((t) => nodeRegistry[t]).filter(
    (def) =>
      (include === undefined || include(def)) &&
      (q === '' ||
        def.label.toLowerCase().includes(q) ||
        def.type.toLowerCase().includes(q) ||
        def.description.toLowerCase().includes(q)),
  );
  return CATEGORY_ORDER.map((category) => ({
    category,
    defs: defs.filter((d) => d.category === category),
  })).filter((g) => g.defs.length > 0);
}
