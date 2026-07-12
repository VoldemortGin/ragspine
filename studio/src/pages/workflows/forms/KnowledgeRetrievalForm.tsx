/** Property form for `knowledge-retrieval` nodes: query, datasets, top_k. */

import { useEffect, useState } from 'react';

import { Field, TextArea, TextInput } from '../../../components';
import type { KnowledgeRetrievalNodeData } from '../../../workflow/types';
import { ValueSelectorInput } from '../shared';
import type { NodeFormProps } from './types';

function parseDatasetIds(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== '');
}

export function KnowledgeRetrievalForm({ data, listId, available, onChange }: NodeFormProps) {
  const typed = data as KnowledgeRetrievalNodeData;

  /* ---------------------------- dataset ids --------------------------- */

  const datasetIds = Array.isArray(typed.dataset_ids)
    ? typed.dataset_ids.filter((id): id is string => typeof id === 'string')
    : [];
  const canonicalIds = datasetIds.join('\n');

  // Local draft so blank lines survive while typing; parsed value is committed.
  const [idsDraft, setIdsDraft] = useState(canonicalIds);
  useEffect(() => {
    setIdsDraft((prev) => (parseDatasetIds(prev).join('\n') === canonicalIds ? prev : canonicalIds));
  }, [canonicalIds]);

  const commitDatasetIds = (raw: string) => {
    setIdsDraft(raw);
    onChange({ ...data, dataset_ids: parseDatasetIds(raw) });
  };

  /* ------------------------------- top_k ------------------------------- */

  const config =
    typeof typed.multiple_retrieval_config === 'object' &&
    typed.multiple_retrieval_config !== null &&
    !Array.isArray(typed.multiple_retrieval_config)
      ? typed.multiple_retrieval_config
      : undefined;
  const configTopK = config !== undefined ? config['top_k'] : undefined;
  const usesConfigTopK = typeof configTopK === 'number' && Number.isFinite(configTopK);
  const flatTopK = typed.top_k;
  const topK = usesConfigTopK
    ? configTopK
    : typeof flatTopK === 'number' && Number.isFinite(flatTopK)
      ? flatTopK
      : 4;

  const commitTopK = (raw: string) => {
    if (raw.trim() === '') {
      if (usesConfigTopK && config !== undefined) {
        const next = { ...config };
        delete next['top_k'];
        onChange({ ...data, multiple_retrieval_config: next });
      } else {
        const next = { ...data };
        delete next['top_k'];
        onChange(next);
      }
      return;
    }
    const n = Number(raw);
    if (!Number.isFinite(n)) return;
    if (usesConfigTopK && config !== undefined) {
      onChange({ ...data, multiple_retrieval_config: { ...config, top_k: n } });
    } else {
      onChange({ ...data, top_k: n });
    }
  };

  return (
    <div className="space-y-3">
      <Field label="Query">
        <ValueSelectorInput
          value={typed.query_variable_selector}
          listId={listId}
          available={available}
          onChange={(selector) => onChange({ ...data, query_variable_selector: selector })}
        />
      </Field>
      <Field label="Dataset ids" hint="One dataset id per line.">
        <TextArea
          value={idsDraft}
          rows={3}
          spellCheck={false}
          placeholder="dataset-id"
          onChange={(e) => commitDatasetIds(e.target.value)}
          className="font-mono !text-xs"
        />
      </Field>
      <Field label="Top K">
        <TextInput
          type="number"
          value={String(topK)}
          onChange={(e) => commitTopK(e.target.value)}
          className="h-7.5 !text-xs"
        />
      </Field>
    </div>
  );
}
