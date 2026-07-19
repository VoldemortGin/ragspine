/** Typed client requests for the workflow catalog/scaffold boundary. */

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ApiError,
  checkWorkflowReadiness,
  getWorkflowTemplate,
  listWorkflowTemplates,
  scaffoldWorkflow,
} from '../../src/api/client';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('workflow catalog client', () => {
  it('posts the current workflow to readiness and forwards cancellation', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        request_id: 'req-ready',
        schema_version: 1,
        status: 'ready',
        checks: {
          format: { status: 'passed' },
          compile: { status: 'passed' },
          runnable: { status: 'passed' },
        },
        start_inputs: [],
        warnings: [],
        requirements: [],
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();

    const response = await checkWorkflowReadiness('app: {}', controller.signal);

    expect(response.status).toBe('ready');
    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/workflow-readiness',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ yaml: 'app: {}' }),
        signal: controller.signal,
      }),
    );
  });

  it('lists metadata with GET and forwards cancellation', async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        jsonResponse({ request_id: 'req-list', templates: [] }),
      );
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();

    const response = await listWorkflowTemplates(controller.signal);

    expect(response.request_id).toBe('req-list');
    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/workflow-templates',
      expect.objectContaining({ method: 'GET', signal: controller.signal }),
    );
  });

  it('follows next_offset through the last page without repeating pages', async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        jsonResponse({
          request_id: 'req-page-1',
          total: 3,
          offset: 0,
          limit: 2,
          next_offset: 2,
          templates: [{ id: 'one' }, { id: 'two' }],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          request_id: 'req-page-2',
          total: 3,
          offset: 2,
          limit: 2,
          next_offset: null,
          templates: [{ id: 'three' }],
        }),
      );
    vi.stubGlobal('fetch', fetchMock);

    const response = await listWorkflowTemplates();

    expect(response.templates.map((template) => template.id)).toEqual([
      'one',
      'two',
      'three',
    ]);
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/v1/workflow-templates?offset=2&limit=2',
      expect.objectContaining({ method: 'GET' }),
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('forwards aborts while fetching a later page', async () => {
    const controller = new AbortController();
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockImplementationOnce(async () => {
        controller.abort();
        return jsonResponse({
          request_id: 'req-page-1',
          total: 2,
          offset: 0,
          limit: 1,
          next_offset: 1,
          templates: [{ id: 'one' }],
        });
      })
      .mockRejectedValueOnce(new DOMException('aborted', 'AbortError'));
    vi.stubGlobal('fetch', fetchMock);

    const error = await listWorkflowTemplates(controller.signal).catch(
      (reason: unknown) => reason,
    );

    expect(error).toMatchObject({ type: 'network_error' });
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/v1/workflow-templates?offset=1&limit=1',
      expect.objectContaining({ signal: controller.signal }),
    );
  });

  it('propagates a failure from a later page', async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        jsonResponse({
          request_id: 'req-page-1',
          total: 2,
          offset: 0,
          limit: 1,
          next_offset: 1,
          templates: [{ id: 'one' }],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          { error: { type: 'catalog_failed', message: 'page failed' } },
          503,
        ),
      );
    vi.stubGlobal('fetch', fetchMock);

    const error = await listWorkflowTemplates().catch(
      (reason: unknown) => reason,
    );

    expect(error).toMatchObject({ status: 503, type: 'catalog_failed' });
  });

  it('rejects a repeated next_offset instead of looping forever', async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        jsonResponse({
          request_id: 'req-page-1',
          total: 10,
          offset: 0,
          limit: 1,
          next_offset: 1,
          templates: [{ id: 'one' }],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          request_id: 'req-page-2',
          total: 10,
          offset: 1,
          limit: 1,
          next_offset: 1,
          templates: [{ id: 'two' }],
        }),
      );
    vi.stubGlobal('fetch', fetchMock);

    const error = await listWorkflowTemplates().catch(
      (reason: unknown) => reason,
    );

    expect(error).toMatchObject({ type: 'invalid_pagination' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('URL-encodes the selected template id', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        request_id: 'req-detail',
        id: 'paper/forms',
        workflow: {},
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await getWorkflowTemplate('paper/forms');

    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/workflow-templates/paper%2Fforms',
      expect.objectContaining({ method: 'GET' }),
    );
  });

  it('posts only the scaffold description and matching controls as JSON', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        request_id: 'req-scaffold',
        origin: 'generated',
        workflow: {},
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await scaffoldWorkflow({
      description: 'A paper form-understanding RAG workflow',
      reuse: true,
    });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [, init] = fetchMock.mock.calls[0]!;
    expect(init).toEqual(
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    expect(JSON.parse(String(init?.body))).toEqual({
      description: 'A paper form-understanding RAG workflow',
      reuse: true,
    });
  });

  it('keeps the common API error shape without exposing it from UI helpers', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(
        {
          error: {
            type: 'WorkflowTemplateNotFound',
            message: 'internal detail',
            request_id: 'req-error',
          },
        },
        404,
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    const error = await getWorkflowTemplate('missing').catch(
      (reason: unknown) => reason,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 404,
      type: 'WorkflowTemplateNotFound',
      requestId: 'req-error',
    });
  });
});
