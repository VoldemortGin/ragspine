/** Read-only fallback form for imported node types outside the palette. */

import { JsonView } from '../../../components';
import { FormHint } from '../shared';
import type { NodeFormProps } from './types';

export function UnknownForm({ data }: NodeFormProps) {
  return (
    <div className="space-y-3">
      <FormHint>
        Node type <span className="font-mono text-zinc-400">{data.type}</span> is not in the
        palette. Its configuration is preserved as-is and exported unchanged.
      </FormHint>
      <JsonView value={data} maxHeight="24rem" />
    </div>
  );
}
