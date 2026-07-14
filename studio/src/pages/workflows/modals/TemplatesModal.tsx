/** Server-backed workflow catalog and natural-language scaffold entry point. */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, FormEvent } from 'react';

import {
  ApiError,
  getWorkflowTemplate,
  listWorkflowTemplates,
  scaffoldWorkflow,
} from '../../../api/client';
import type { WorkflowTemplateSummary } from '../../../api/types';
import {
  Badge,
  Button,
  EmptyState,
  IconAlertTriangle,
  IconSearch,
  IconSparkle,
  Modal,
  Select,
  Spinner,
  TextArea,
  TextInput,
} from '../../../components';
import {
  BLANK_WORKFLOW_TEMPLATE,
  SCAFFOLD_DATA_FLOW_NOTICE,
  TEMPLATE_PROVENANCE_NOTICE,
  detailToCreatableTemplate,
  filterWorkflowTemplates,
  normalizeWorkflowTemplateSummaries,
  scaffoldToCreatableTemplate,
  templateCompatibilityLabel,
  templateRequirementLabels,
  templateSourceView,
  workflowTemplateCategories,
  workflowTemplatePlatforms,
} from '../model/templates';
import { useEditorStore } from '../store';

export interface TemplatesModalProps {
  open: boolean;
  onClose: () => void;
}

function safeRequestError(error: unknown, action: 'catalog' | 'template' | 'scaffold'): string {
  if (error instanceof ApiError) {
    if (error.type === 'network_error') {
      return 'Could not reach the RAGSpine server. Blank remains available offline.';
    }
    if (action === 'template' && error.status === 404) {
      return 'That template is no longer available. Refresh the catalog and try again.';
    }
    if (action === 'scaffold' && error.status === 422) {
      return 'The description was rejected. Use between 1 and 4096 characters and try again.';
    }
    return `The server could not complete this request (HTTP ${String(error.status)}).`;
  }
  return action === 'catalog'
    ? 'The catalog response could not be loaded.'
    : 'The server returned a workflow document that Studio could not import.';
}

function BlankCard({ disabled, onPick }: { disabled: boolean; onPick: () => void }) {
  return (
    <article className="rounded-lg border border-indigo-400/25 bg-indigo-400/[0.04] p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-medium text-zinc-100">{BLANK_WORKFLOW_TEMPLATE.name}</div>
          <p className="mt-1 text-xs leading-5 text-zinc-500">
            Offline starter: start → LLM → end. No server or template catalog required.
          </p>
        </div>
        <Badge variant="accent">local</Badge>
      </div>
      <Button className="mt-3 w-full" size="sm" disabled={disabled} onClick={onPick}>
        Create blank workflow
      </Button>
    </article>
  );
}

function TemplateCard({
  template,
  loading,
  disabled,
  onPick,
}: {
  template: WorkflowTemplateSummary;
  loading: boolean;
  disabled: boolean;
  onPick: (template: WorkflowTemplateSummary) => void;
}) {
  const source = templateSourceView(template);
  const requirements = templateRequirementLabels(template);
  const pick = useCallback(() => onPick(template), [onPick, template]);
  const status = template.compatibility.status.toLocaleLowerCase();
  const supported = status === 'runnable' || status === 'supported';

  return (
    <article className="flex min-h-52 flex-col rounded-lg border border-white/10 bg-white/[0.02] p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-zinc-200">{template.name}</div>
          <div className="mt-1 line-clamp-3 text-xs leading-5 text-zinc-500">
            {template.description || 'No description provided.'}
          </div>
        </div>
        {source.platform !== '' && <Badge variant="info">{source.platform}</Badge>}
      </div>

      <div className="mt-2 flex flex-wrap gap-1">
        <Badge variant={supported ? 'success' : 'warn'}>
          {templateCompatibilityLabel(template)}
        </Badge>
        {template.categories.map((category) => (
          <Badge key={category}>{category}</Badge>
        ))}
      </div>

      <div className="mt-2 space-y-1 text-[11px] leading-4 text-zinc-500">
        <div className="text-zinc-400">{TEMPLATE_PROVENANCE_NOTICE}</div>
        {(source.author !== '' || source.title !== '') && (
          <div className="truncate">
            Upstream reference:{' '}
            {[source.author, source.title].filter((part) => part !== '').join(' · ')}
          </div>
        )}
        {(source.popularity !== '' || source.observedAt !== '') && (
          <div className="truncate">
            Observed:{' '}
            {[source.popularity, source.observedAt].filter((part) => part !== '').join(' · ')}
          </div>
        )}
        {requirements.length > 0 && (
          <div className="line-clamp-2">Requires: {requirements.join(', ')}</div>
        )}
        {source.licenseStatus !== '' && (
          <div className="truncate">Upstream config: {source.licenseStatus}</div>
        )}
      </div>

      <div className="mt-auto flex items-end justify-between gap-2 pt-3">
        {source.httpsUrl !== null ? (
          <a
            href={source.httpsUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="truncate text-[11px] text-sky-400 hover:text-sky-300"
          >
            View upstream reference
          </a>
        ) : (
          <span className="text-[11px] text-zinc-600">
            {source.licenseStatus !== '' ? source.licenseStatus : 'Built-in catalog'}
          </span>
        )}
        <Button size="sm" loading={loading} disabled={disabled || !supported} onClick={pick}>
          {supported ? 'Use template' : 'Unavailable'}
        </Button>
      </div>
    </article>
  );
}

export function TemplatesModal({ open, onClose }: TemplatesModalProps) {
  const createWorkflow = useEditorStore((state) => state.createWorkflow);
  const createFromTemplate = useEditorStore((state) => state.createFromTemplate);
  const [description, setDescription] = useState('');
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState('');
  const [platform, setPlatform] = useState('');
  const [templates, setTemplates] = useState<WorkflowTemplateSummary[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [loadingTemplateId, setLoadingTemplateId] = useState<string | null>(null);
  const [scaffolding, setScaffolding] = useState(false);
  const [reload, setReload] = useState(0);
  const actionController = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    let active = true;
    setCatalogLoading(true);
    setCatalogError(null);
    void listWorkflowTemplates(controller.signal)
      .then((response) => {
        if (!active) return;
        const raw = (response as unknown as Record<string, unknown>)['templates'];
        setTemplates(normalizeWorkflowTemplateSummaries(raw));
      })
      .catch((error: unknown) => {
        if (!active) return;
        setTemplates([]);
        setCatalogError(safeRequestError(error, 'catalog'));
      })
      .finally(() => {
        if (active) setCatalogLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [open, reload]);

  useEffect(() => {
    if (open) {
      setDescription('');
      setQuery('');
      setCategory('');
      setPlatform('');
      setActionError(null);
      return;
    }
    actionController.current?.abort();
    actionController.current = null;
    setLoadingTemplateId(null);
    setScaffolding(false);
  }, [open]);

  const categories = useMemo(() => workflowTemplateCategories(templates), [templates]);
  const platforms = useMemo(() => workflowTemplatePlatforms(templates), [templates]);
  const filtered = useMemo(
    () => filterWorkflowTemplates(templates, { query, category, platform }),
    [category, platform, query, templates],
  );
  const actionBusy = scaffolding || loadingTemplateId !== null;

  const close = useCallback(() => {
    actionController.current?.abort();
    actionController.current = null;
    onClose();
  }, [onClose]);

  const pickBlank = useCallback(() => {
    createWorkflow();
    close();
  }, [close, createWorkflow]);

  const pickTemplate = useCallback(
    (template: WorkflowTemplateSummary) => {
      if (actionBusy || actionController.current !== null) return;
      const controller = new AbortController();
      actionController.current = controller;
      setActionError(null);
      setLoadingTemplateId(template.id);
      void getWorkflowTemplate(template.id, controller.signal)
        .then((detail) => {
          const creatable = detailToCreatableTemplate(detail);
          createFromTemplate(creatable);
          close();
        })
        .catch((error: unknown) => {
          if (!controller.signal.aborted) setActionError(safeRequestError(error, 'template'));
        })
        .finally(() => {
          if (actionController.current === controller) actionController.current = null;
          if (!controller.signal.aborted) setLoadingTemplateId(null);
        });
    },
    [actionBusy, close, createFromTemplate],
  );

  const submitScaffold = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const prompt = description.trim();
      if (prompt === '' || actionBusy || actionController.current !== null) return;
      const controller = new AbortController();
      actionController.current = controller;
      setActionError(null);
      setScaffolding(true);
      void scaffoldWorkflow({ description: prompt, reuse: true }, controller.signal)
        .then((response) => {
          const creatable = scaffoldToCreatableTemplate(response, prompt);
          createFromTemplate(creatable);
          close();
        })
        .catch((error: unknown) => {
          if (!controller.signal.aborted) setActionError(safeRequestError(error, 'scaffold'));
        })
        .finally(() => {
          if (actionController.current === controller) actionController.current = null;
          if (!controller.signal.aborted) setScaffolding(false);
        });
    },
    [actionBusy, close, createFromTemplate, description],
  );

  const changeDescription = useCallback((event: ChangeEvent<HTMLTextAreaElement>) => {
    setDescription(event.target.value);
    setActionError(null);
  }, []);
  const changeQuery = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setQuery(event.target.value);
  }, []);
  const changeCategory = useCallback((event: ChangeEvent<HTMLSelectElement>) => {
    setCategory(event.target.value);
  }, []);
  const changePlatform = useCallback((event: ChangeEvent<HTMLSelectElement>) => {
    setPlatform(event.target.value);
  }, []);
  const retryCatalog = useCallback(() => setReload((value) => value + 1), []);

  return (
    <Modal open={open} onClose={close} title="Create workflow" size="wide">
      <div className="space-y-5">
        <form
          className="rounded-xl border border-indigo-400/20 bg-indigo-400/[0.04] p-4"
          onSubmit={submitScaffold}
        >
          <div className="flex items-center gap-2 text-sm font-medium text-zinc-200">
            <IconSparkle size={15} className="text-indigo-300" />
            Describe the workflow you need
          </div>
          <p className="mt-1 text-xs leading-5 text-zinc-500">{SCAFFOLD_DATA_FLOW_NOTICE}</p>
          <TextArea
            className="mt-3"
            rows={3}
            value={description}
            minLength={1}
            maxLength={4096}
            aria-label="Workflow description"
            placeholder="A RAG form-understanding workflow for a CNN research paper"
            disabled={actionBusy}
            onChange={changeDescription}
          />
          <div className="mt-2 flex items-center justify-between gap-3">
            <span className="font-mono text-[10px] text-zinc-600">
              {String(description.length)} / 4096
            </span>
            <Button
              type="submit"
              variant="primary"
              loading={scaffolding}
              disabled={description.trim() === '' || actionBusy}
            >
              Create workflow
            </Button>
          </div>
        </form>

        {actionError !== null && (
          <div className="rounded-lg border border-red-400/25 bg-red-400/[0.06] px-4 py-3">
            <div className="flex items-center gap-2 text-sm font-medium text-red-300">
              <IconAlertTriangle size={14} />
              Could not create workflow
            </div>
            <div className="mt-1 text-xs leading-5 text-zinc-400">{actionError}</div>
          </div>
        )}

        <section aria-labelledby="template-gallery-heading">
          <div className="flex items-end justify-between gap-3">
            <div>
              <h3 id="template-gallery-heading" className="text-sm font-medium text-zinc-200">
                Template gallery
              </h3>
              <p className="mt-1 text-xs text-zinc-500">
                Selecting a template imports its workflow definition into your local library.
              </p>
            </div>
            <span className="text-[11px] text-zinc-600">
              {String(filtered.length)} catalog templates
            </span>
          </div>

          <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
            <div className="relative sm:col-span-1">
              <IconSearch
                size={13}
                className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-zinc-600"
              />
              <TextInput
                value={query}
                onChange={changeQuery}
                aria-label="Search workflow templates"
                placeholder="Search templates"
                className="pl-8"
              />
            </div>
            <Select value={category} onChange={changeCategory} aria-label="Filter by category">
              <option value="">All categories</option>
              {categories.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </Select>
            <Select value={platform} onChange={changePlatform} aria-label="Filter by platform">
              <option value="">All platforms</option>
              {platforms.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </Select>
          </div>

          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <BlankCard disabled={actionBusy} onPick={pickBlank} />
            {filtered.map((template) => (
              <TemplateCard
                key={template.id}
                template={template}
                loading={loadingTemplateId === template.id}
                disabled={actionBusy}
                onPick={pickTemplate}
              />
            ))}
          </div>

          {catalogLoading && (
            <div className="flex items-center justify-center gap-2 py-10 text-xs text-zinc-500">
              <Spinner size="sm" /> Loading catalog…
            </div>
          )}

          {!catalogLoading && catalogError !== null && (
            <div className="mt-3 flex items-center justify-between gap-3 rounded-lg border border-amber-400/25 bg-amber-400/[0.05] px-4 py-3">
              <div className="text-xs leading-5 text-zinc-400">{catalogError}</div>
              <Button size="sm" variant="ghost" onClick={retryCatalog}>
                Retry
              </Button>
            </div>
          )}

          {!catalogLoading &&
            catalogError === null &&
            templates.length > 0 &&
            filtered.length === 0 && (
              <EmptyState
                icon={<IconSearch size={18} />}
                title="No matching templates"
                hint="Try another search term, category, or source platform."
                className="py-8"
              />
            )}

          {!catalogLoading && catalogError === null && templates.length === 0 && (
            <EmptyState
              title="The server catalog is empty"
              hint="You can still create a blank workflow or describe one above."
              className="py-8"
            />
          )}
        </section>
      </div>
    </Modal>
  );
}
