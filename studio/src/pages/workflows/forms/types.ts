/** Contract between the property panel and the per-node-type forms. */

import type { StudioNodeData } from '../../../workflow/types';
import type { AvailableVariable } from '../model/variables';

export interface NodeFormProps {
  nodeId: string;
  /** The node's full Dify data object (known + unknown keys). */
  data: StudioNodeData;
  /** All node ids on the canvas (for value-selector suggestions). */
  nodeIds: readonly string[];
  /** DOM id of a <datalist> of upstream node ids rendered by the panel. */
  listId: string;
  /** Output variables of the node's topological upstream (model/variables). */
  available: readonly AvailableVariable[];
  /**
   * Replace the node's data object. MUST be an immutable update that spreads
   * the previous data so unknown keys are preserved.
   */
  onChange: (next: StudioNodeData) => void;
}
