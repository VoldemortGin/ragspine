/** Typed client requests for the workflow catalog/scaffold boundary. */

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ApiError,
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
