/** Property form for `llm` nodes: model config, prompt messages, context. */

import { useState } from 'react';

import {
  Checkbox,
  Field,
  IconArrowDown,
  IconArrowUp,
  Select,
  TextInput,
} from '../../../components';
import type { LlmNodeData, ModelConfig, PromptMessage } from '../../../workflow/types';
import { VariableTextArea } from '../VariableTextArea';
import { AddButton, FormHint, ListRow, ValueSelectorInput } from '../shared';
import type { NodeFormProps } from './types';

const ROLES = ['system', 'user', 'assistant'];

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function LlmForm({ data, listId, available, onChange }: NodeFormProps) {
  const typed = data as LlmNodeData;
  const [contextOpen, setContextOpen] = useState(false);

  /* ------------------------------- model ------------------------------ */

  const model = asRecord(typed.model) as ModelConfig;
  const completionParams = asRecord(model.completion_params);
  const maxTokensRaw = completionParams['max_tokens'];
  const maxTokens =
    typeof maxTokensRaw === 'number' && Number.isFinite(maxTokensRaw) ? maxTokensRaw : undefined;

  const patchModel = (changes: Partial<ModelConfig>) =>
    onChange({ ...data, model: { ...model, ...changes } });

  const commitMaxTokens = (raw: string) => {
    const params = { ...completionParams };
    if (raw.trim() === '') {
      delete params['max_tokens'];
    } else {
      const n = Number(raw);
      if (!Number.isFinite(n)) return;
      params['max_tokens'] = n;
    }
    onChange({ ...data, model: { ...model, completion_params: params } });
  };

  /* --------------------------- prompt messages ------------------------ */

  const messages = Array.isArray(typed.prompt_template) ? typed.prompt_template : [];

  const commitMessages = (next: PromptMessage[]) => onChange({ ...data, prompt_template: next });
  const patchMessage = (index: number, changes: Partial<PromptMessage>) =>
    commitMessages(messages.map((row, i) => (i === index ? { ...row, ...changes } : row)));
  const moveMessage = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= messages.length) return;
    const next = [...messages];
    const a = next[index];
    next[index] = next[target];
    next[target] = a;
    commitMessages(next);
  };

  /* ------------------------------ context ----------------------------- */

  const context = asRecord(typed.context);
  const contextEnabled = context['enabled'] === true;
  const patchContext = (changes: Record<string, unknown>) =>
    onChange({ ...data, context: { ...context, ...changes } });

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <Field label="Provider">
          <TextInput
            value={typeof model.provider === 'string' ? model.provider : ''}
            placeholder="openai"
            onChange={(e) => patchModel({ provider: e.target.value })}
            className="h-7.5 !text-xs"
          />
        </Field>
        <Field label="Model">
          <TextInput
            value={typeof model.name === 'string' ? model.name : ''}
            placeholder="gpt-4o"
            onChange={(e) => patchModel({ name: e.target.value })}
            className="h-7.5 font-mono !text-xs"
          />
        </Field>
      </div>
      <Field label="Max tokens" hint="Leave empty for the provider default.">
        <TextInput
          type="number"
          value={maxTokens !== undefined ? String(maxTokens) : ''}
          placeholder="default"
          onChange={(e) => commitMaxTokens(e.target.value)}
          className="h-7.5 !text-xs"
        />
      </Field>

      <div>
        <div className="mb-1.5 text-xs font-medium text-zinc-400">Prompt messages</div>
        <div className="space-y-1.5">
          {messages.map((row, index) => {
            const role: string = typeof row.role === 'string' ? row.role : 'user';
            const roleOptions = ROLES.includes(role) ? ROLES : [...ROLES, role];
            return (
              <ListRow key={index} onRemove={() => commitMessages(messages.filter((_, i) => i !== index))}>
                <div className="flex items-center gap-1.5">
                  <div className="w-28 shrink-0">
                    <Select
                      value={role}
                      onChange={(e) =>
                        patchMessage(index, { role: e.target.value as PromptMessage['role'] })
                      }
                      className="h-7.5 !text-xs"
                    >
                      {roleOptions.map((r) => (
                        <option key={r} value={r}>
                          {r}
                        </option>
                      ))}
                    </Select>
                  </div>
                  <div className="flex-1" />
                  <button
                    type="button"
                    title="Move up"
                    disabled={index === 0}
                    onClick={() => moveMessage(index, -1)}
                    className="rounded p-1 text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-300 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent"
                  >
                    <IconArrowUp size={12} />
                  </button>
                  <button
                    type="button"
                    title="Move down"
                    disabled={index === messages.length - 1}
                    onClick={() => moveMessage(index, 1)}
                    className="rounded p-1 text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-300 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent"
                  >
                    <IconArrowDown size={12} />
                  </button>
                </div>
                <VariableTextArea
                  value={typeof row.text === 'string' ? row.text : ''}
                  rows={3}
                  placeholder="Message text"
                  available={available}
                  onChange={(text) => patchMessage(index, { text })}
                  className="font-mono !text-xs"
                />
              </ListRow>
            );
          })}
          <AddButton onClick={() => commitMessages([...messages, { role: 'user', text: '' }])}>
            Add message
          </AddButton>
        </div>
      </div>

      <div>
        <button
          type="button"
          onClick={() => setContextOpen((open) => !open)}
          className="text-xs font-medium text-zinc-400 transition-colors hover:text-zinc-200"
        >
          Context {contextOpen ? '(hide)' : '(show)'}
        </button>
        {contextOpen && (
          <div className="mt-1.5 space-y-1.5 rounded-md border border-white/5 bg-white/[0.02] p-2">
            <Checkbox
              checked={contextEnabled}
              onChange={(e) => patchContext({ enabled: e.target.checked })}
              label="Enable context"
              className="!text-xs"
            />
            <ValueSelectorInput
              value={context['variable_selector']}
              listId={listId}
              available={available}
              disabled={!contextEnabled}
              onChange={(selector) => patchContext({ variable_selector: selector })}
            />
            <FormHint>Selected variable is exposed to the prompt as the context block.</FormHint>
          </div>
        )}
      </div>
    </div>
  );
}
