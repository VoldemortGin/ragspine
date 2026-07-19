/** Pure helpers for the server-backed workflow-template catalog. */

import type {
  WorkflowScaffoldResponse,
  WorkflowTemplateCompatibility,
  WorkflowTemplateDetail,
  WorkflowTemplateRequirement,
  WorkflowTemplateSource,
  WorkflowTemplateSummary,
} from '../../../api/types';
import { parseWorkflowYaml, serializeWorkflowYaml } from '../../../workflow/convert';
import type { StudioWorkflow } from '../../../workflow/types';
import { createTemplateWorkflow } from './template';

export interface CreatableWorkflowTemplate {
  name: string;
  yaml: string;
}

export interface WorkflowTemplateFilters {
  query: string;
  category: string;
  platform: string;
}

export interface WorkflowTemplateSourceView {
  platform: string;
  title: string;
  author: string;
  popularity: string;
  observedAt: string;
  licenseStatus: string;
  httpsUrl: string | null;
}

export interface WorkflowDeploymentReadiness {
  kind: 'ready' | 'needs-provider' | 'needs-setup' | 'blocked';
  label: 'Ready' | 'Needs provider' | 'Needs setup' | 'Blocked';
  detail: string;
}

interface WorkflowScaffoldMetadata {
  request_id: string;
  origin: WorkflowScaffoldResponse['origin'];
  template_id: string | null;
  confidence: number;
  matcher: string;
  warnings: string[];
  compatibility: WorkflowTemplateCompatibility;
  requirements: WorkflowTemplateRequirement[];
  source: WorkflowTemplateSource | null;
}

const SCAFFOLD_METADATA_KEY = 'x-ragspine-scaffold';

export const TEMPLATE_PROVENANCE_NOTICE =
  'Spine-authored equivalent · upstream reference only · config not redistributed';
export const SCAFFOLD_DATA_FLOW_NOTICE =
  'Studio sends this description to the configured RAGSpine server, which returns a Dify-compatible definition. Studio saves it locally and does not execute it.';

/** The only local catalog entry: an offline-safe fallback when the API is unavailable. */
export const BLANK_WORKFLOW_TEMPLATE: Readonly<CreatableWorkflowTemplate & { id: 'blank' }> = {
  id: 'blank',
  name: 'Blank',
  yaml: serializeWorkflowYaml(createTemplateWorkflow('Blank')),
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function safeString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function safeStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map(safeString).filter((item) => item !== '');
}

function safeSource(value: unknown): WorkflowTemplateSource | null {
  return isRecord(value) ? (value as unknown as WorkflowTemplateSource) : null;
}

function normalizeCompatibility(value: unknown): WorkflowTemplateSummary['compatibility'] {
  if (!isRecord(value)) return { format: '', dsl_version: '', status: '' };
  return {
    format: safeString(value['format']),
    dsl_version: safeString(value['dsl_version']),
    status: safeString(value['status']),
  };
}

function normalizeRequirements(value: unknown): WorkflowTemplateSummary['requirements'] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!isRecord(item)) return [];
    const name = safeString(item['name']);
    if (name === '') return [];
    return [
      {
        kind: safeString(item['kind']),
        name,
        required: item['required'] === true,
      },
    ];
  });
}

function normalizeSource(value: unknown): WorkflowTemplateSource | null {
  if (!isRecord(value)) return null;
  return {
    provider: safeString(value['provider']),
    title: safeString(value['title']),
    author: safeString(value['author']) || null,
    upstream_id: safeString(value['upstream_id']) || null,
    upstream_url: safeString(value['upstream_url']) || null,
    license_status: safeString(value['license_status']),
    observed_metric: safeString(value['observed_metric']) || null,
    observed_value:
      typeof value['observed_value'] === 'number' && Number.isFinite(value['observed_value'])
        ? value['observed_value']
        : null,
    observed_at: safeString(value['observed_at']) || null,
  };
}

/**
 * Narrow an untrusted list response to render-safe catalog summaries.
 * Entries without a stable id/name are dropped; nested fields are normalized.
 */
export function normalizeWorkflowTemplateSummaries(value: unknown): WorkflowTemplateSummary[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!isRecord(item)) return [];
    const id = safeString(item['id']);
    const name = safeString(item['name']);
    if (id === '' || name === '') return [];
    const sha256 = safeString(item['sha256']);
    return [
      {
        id,
        name,
        description: safeString(item['description']),
        categories: safeStrings(item['categories']),
        tags: safeStrings(item['tags']),
        intents: safeStrings(item['intents']),
        examples: safeStrings(item['examples']),
        compatibility: normalizeCompatibility(item['compatibility']),
        requirements: normalizeRequirements(item['requirements']),
        source: normalizeSource(item['source']),
        ...(sha256 !== '' ? { sha256 } : {}),
      },
    ];
  });
}

function requirementLabel(value: unknown): string {
  if (!isRecord(value)) return '';
  const name = safeString(value['name']);
  const kind = safeString(value['kind']);
  if (name === '') return kind;
  return kind === '' ? name : `${kind}: ${name}`;
}

/** Runtime-defensive requirement labels: malformed server fields are ignored, never rendered. */
export function templateRequirementLabels(template: WorkflowTemplateSummary): string[] {
  const raw: unknown = template.requirements;
  if (!Array.isArray(raw)) return [];
  return raw.map(requirementLabel).filter((label) => label !== '');
}

/** Only permit ordinary HTTPS links from catalog provenance metadata. */
export function safeHttpsUrl(value: unknown): string | null {
  const raw = safeString(value);
  if (raw === '') return null;
  try {
    const url = new URL(raw);
    return url.protocol === 'https:' ? url.toString() : null;
  } catch {
    return null;
  }
}

/** Render-ready provenance fields with strict runtime guards at the API boundary. */
export function templateSourceView(template: WorkflowTemplateSummary): WorkflowTemplateSourceView {
  const source = safeSource(template.source);
  if (source === null) {
    return {
      platform: '',
      title: '',
      author: '',
      popularity: '',
      observedAt: '',
      licenseStatus: '',
      httpsUrl: null,
    };
  }
  const observedMetric = safeString(source.observed_metric);
  const observedValue =
    typeof source.observed_value === 'number' && Number.isFinite(source.observed_value)
      ? source.observed_value
      : null;
  return {
    platform: safeString(source.provider),
    title: safeString(source.title),
    author: safeString(source.author),
    popularity:
      observedMetric !== '' && observedValue !== null
        ? `${observedMetric}: ${String(observedValue)}`
        : '',
    observedAt: safeString(source.observed_at),
    licenseStatus: safeString(source.license_status),
    httpsUrl: safeHttpsUrl(source.upstream_url),
  };
}

/** Compact compatibility label; malformed objects degrade to "Compatibility unknown". */
export function templateCompatibilityLabel(template: WorkflowTemplateSummary): string {
  const value: unknown = template.compatibility;
  if (!isRecord(value)) return 'Compatibility unknown';
  const format = safeString(value['format']);
  const version = safeString(value['dsl_version']);
  const status = safeString(value['status']);
  const parts = [format, version, status].filter((part) => part !== '');
  return parts.length > 0 ? parts.join(' · ') : 'Compatibility unknown';
}

function popularityValue(template: WorkflowTemplateSummary): number | null {
  const source = safeSource(template.source);
  return source !== null &&
    typeof source.observed_value === 'number' &&
    Number.isFinite(source.observed_value)
    ? source.observed_value
    : null;
}

/** Popular templates first; missing/malformed popularity sorts last, then by stable name/id. */
export function sortWorkflowTemplates(
  templates: readonly WorkflowTemplateSummary[],
): WorkflowTemplateSummary[] {
  return [...templates].sort((left, right) => {
    const leftPopularity = popularityValue(left);
    const rightPopularity = popularityValue(right);
    if (leftPopularity !== rightPopularity) {
      if (leftPopularity === null) return 1;
      if (rightPopularity === null) return -1;
      return rightPopularity - leftPopularity;
    }
    const byName = safeString(left.name).localeCompare(safeString(right.name));
    return byName !== 0 ? byName : safeString(left.id).localeCompare(safeString(right.id));
  });
}

function searchableText(template: WorkflowTemplateSummary): string {
  const source = templateSourceView(template);
  return [
    safeString(template.name),
    safeString(template.description),
    ...safeStrings(template.categories),
    ...safeStrings(template.tags),
    ...safeStrings(template.intents),
    ...safeStrings(template.examples),
    ...templateRequirementLabels(template),
    source.platform,
    source.title,
    source.author,
    templateCompatibilityLabel(template),
  ]
    .join(' ')
    .toLocaleLowerCase();
}

export function filterWorkflowTemplates(
  templates: readonly WorkflowTemplateSummary[],
  filters: WorkflowTemplateFilters,
): WorkflowTemplateSummary[] {
  const query = filters.query.trim().toLocaleLowerCase();
  const category = filters.category.trim().toLocaleLowerCase();
  const platform = filters.platform.trim().toLocaleLowerCase();

  return sortWorkflowTemplates(
    templates.filter((template) => {
      const categories = safeStrings(template.categories).map((item) => item.toLocaleLowerCase());
      const sourcePlatform = templateSourceView(template).platform.toLocaleLowerCase();
      return (
        (query === '' || searchableText(template).includes(query)) &&
        (category === '' || categories.includes(category)) &&
        (platform === '' || sourcePlatform === platform)
      );
    }),
  );
}

function uniqueSorted(values: readonly string[]): string[] {
  return [...new Set(values.filter((item) => item !== ''))].sort((a, b) => a.localeCompare(b));
}

export function workflowTemplateCategories(
  templates: readonly WorkflowTemplateSummary[],
): string[] {
  return uniqueSorted(templates.flatMap((template) => safeStrings(template.categories)));
}

export function workflowTemplatePlatforms(templates: readonly WorkflowTemplateSummary[]): string[] {
  return uniqueSorted(templates.map((template) => templateSourceView(template).platform));
}

function workflowDocumentText(response: {
  workflow?: Record<string, unknown>;
  yaml?: string;
}): string {
  if (isRecord(response.workflow)) return JSON.stringify(response.workflow);
  const yaml = safeString(response.yaml);
  if (yaml !== '') return yaml;
  throw new Error('Workflow response did not contain a workflow document.');
}

function fallbackWorkflowName(description: string): string {
  const normalized = description.trim().replace(/\s+/g, ' ');
  return normalized === '' ? 'Generated workflow' : normalized.slice(0, 72);
}

/** Validate a detail payload before handing it to the existing local library action. */
export function detailToCreatableTemplate(
  detail: WorkflowTemplateDetail,
): CreatableWorkflowTemplate {
  const yaml = workflowDocumentText(detail);
  const parsed = parseWorkflowYaml(yaml);
  const name = safeString(detail.name) || safeString(parsed.name) || 'Workflow template';
  return { name, yaml };
}

/** Prefer canonical JSON workflow data, with legacy YAML only as a compatibility fallback. */
export function scaffoldToCreatableTemplate(
  response: WorkflowScaffoldResponse,
  description: string,
): CreatableWorkflowTemplate {
  const yaml = workflowDocumentText(response);
  const parsed = parseWorkflowYaml(yaml);
  const name = safeString(parsed.name) || fallbackWorkflowName(description);
  const metadata: WorkflowScaffoldMetadata = {
    request_id: safeString(response.request_id),
    origin: response.origin === 'template' ? 'template' : 'generated',
    template_id: safeString(response.template_id) || null,
    confidence:
      typeof response.confidence === 'number' && Number.isFinite(response.confidence)
        ? response.confidence
        : 0,
    matcher: safeString(response.matcher),
    warnings: safeStrings(response.warnings),
    compatibility: normalizeCompatibility(response.compatibility),
    requirements: normalizeRequirements(response.requirements),
    source: normalizeSource(response.source),
  };
  const workflow: StudioWorkflow = {
    ...parsed,
    docPassthrough: {
      ...parsed.docPassthrough,
      [SCAFFOLD_METADATA_KEY]: metadata,
    },
  };
  return { name, yaml: serializeWorkflowYaml(workflow) };
}

/** Deployment state persisted inside scaffold-created workflow YAML. */
export function workflowDeploymentReadiness(
  workflow: StudioWorkflow,
): WorkflowDeploymentReadiness | null {
  const raw = workflow.docPassthrough[SCAFFOLD_METADATA_KEY];
  if (!isRecord(raw)) return null;

  const compatibility = normalizeCompatibility(raw['compatibility']);
  const requirements = normalizeRequirements(raw['requirements']).filter(
    (requirement) => requirement.required,
  );
  const warnings = safeStrings(raw['warnings']);
  const context = [
    `Origin: ${safeString(raw['origin']) || 'unknown'}`,
    `Compatibility: ${compatibility.status || 'unknown'}`,
    ...(warnings.length > 0 ? [`Warnings: ${warnings.join('; ')}`] : []),
  ];
  const status = compatibility.status.toLocaleLowerCase();
  if (status !== 'runnable' && status !== 'supported' && status !== 'compatible') {
    return { kind: 'blocked', label: 'Blocked', detail: context.join(' · ') };
  }
  if (requirements.length === 0) {
    return { kind: 'ready', label: 'Ready', detail: context.join(' · ') };
  }

  const requirementNames = requirements.map((requirement) => requirementLabel(requirement));
  const detail = [...context, `Requires: ${requirementNames.join(', ')}`].join(' · ');
  const providerOnly = requirements.every((requirement) => {
    const kind = requirement.kind.toLocaleLowerCase();
    return kind.includes('provider') || kind === 'model';
  });
  return providerOnly
    ? { kind: 'needs-provider', label: 'Needs provider', detail }
    : { kind: 'needs-setup', label: 'Needs setup', detail };
}
