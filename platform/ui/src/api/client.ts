import type {
  ControlChange,
  ControlPreview,
  ControlState,
  EventsResponse,
  HealthSnapshot,
  MeshManifest,
  RunDetail,
  SessionDetail,
  SessionListResponse,
} from './types';

const apiBase = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '');

interface ApiErrorBody {
  error?: {
    code?: string;
    message?: string;
    details?: unknown;
  };
  detail?: string | { message?: string };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, body: ApiErrorBody) {
    const fallback = status === 0 ? '无法连接服务器，请检查内网服务是否已启动。' : `请求失败（HTTP ${status}）`;
    const detailMessage = typeof body.detail === 'string' ? body.detail : body.detail?.message;
    super(body.error?.message ?? detailMessage ?? fallback);
    this.name = 'ApiError';
    this.status = status;
    this.code = body.error?.code ?? `HTTP_${status}`;
    this.details = body.error?.details;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${apiBase}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...init?.headers,
      },
    });
  } catch {
    throw new ApiError(0, {});
  }

  if (!response.ok) {
    let body: ApiErrorBody = {};
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      // 非 JSON 反向代理错误仍转换为稳定的前端错误。
    }
    throw new ApiError(response.status, body);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

function json(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}

export function newRequestId(): string {
  if ('randomUUID' in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export const api = {
  getHealth(): Promise<HealthSnapshot> {
    return request('/api/health');
  },

  async listSessions(status?: string, cursor?: string): Promise<SessionListResponse> {
    const params = new URLSearchParams();
    if (status && status !== 'ALL') params.set('status', status);
    if (cursor) params.set('cursor', cursor);
    const raw = await request<SessionListResponse | SessionListResponse['items']>(
      `/api/v1/sessions${params.size ? `?${params}` : ''}`,
    );
    return Array.isArray(raw) ? { items: raw } : raw;
  },

  async createSession(input: {
    file: File;
    title: string;
    expertSignature?: string;
  }): Promise<SessionDetail> {
    const form = new FormData();
    form.append('file', input.file);
    form.append('title', input.title);
    if (input.expertSignature) form.append('expert_signature', input.expertSignature);
    return request('/api/v1/sessions', { method: 'POST', body: form });
  },

  getSession(sessionId: string): Promise<SessionDetail> {
    return request(`/api/v1/sessions/${encodeURIComponent(sessionId)}`);
  },

  getRun(runId: string): Promise<RunDetail> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}`);
  },

  getControlState(sessionId: string, parentRunId: string): Promise<ControlState> {
    const query = new URLSearchParams({ parent_run_id: parentRunId });
    return request(`/api/v1/sessions/${encodeURIComponent(sessionId)}/control-state?${query}`);
  },

  previewControls(
    sessionId: string,
    parentRunId: string,
    changes: ControlChange[],
  ): Promise<ControlPreview> {
    return request(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/control-preview`,
      json('POST', { parent_run_id: parentRunId, changes }),
    );
  },

  createRun(input: {
    sessionId: string;
    parentRunId: string;
    changes: ControlChange[];
    expectedVersion: number;
    requestId: string;
    confirmRequiredClears?: boolean;
  }): Promise<RunDetail> {
    return request(
      `/api/v1/sessions/${encodeURIComponent(input.sessionId)}/runs`,
      json('POST', {
        parent_run_id: input.parentRunId,
        changes: input.changes,
        expected_version: input.expectedVersion,
        request_id: input.requestId,
        confirm_required_clears: input.confirmRequiredClears ?? false,
      }),
    );
  },

  retryRun(runId: string, expectedVersion: number, requestId: string): Promise<RunDetail> {
    return request(
      `/api/v1/runs/${encodeURIComponent(runId)}/retry`,
      json('POST', { expected_version: expectedVersion, request_id: requestId }),
    );
  },

  updateExperienceNote(runId: string, note: string, expectedNoteVersion: number): Promise<RunDetail> {
    return request(
      `/api/v1/runs/${encodeURIComponent(runId)}/experience-note`,
      json('PUT', { note, expected_note_version: expectedNoteVersion }),
    );
  },

  completeSession(sessionId: string, runId: string, expectedVersion: number): Promise<SessionDetail> {
    return request(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/complete`,
      json('POST', { run_id: runId, expected_version: expectedVersion }),
    );
  },

  getEvents(runId: string, after = 0): Promise<EventsResponse> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/events?after=${after}`);
  },

  getMeshManifest(runId: string): Promise<MeshManifest> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/mesh/manifest`);
  },

  meshBlockUrl(runId: string, blockId: string, mode: 'surface' | 'wireframe'): string {
    return `${apiBase}/api/v1/runs/${encodeURIComponent(runId)}/mesh/blocks/${encodeURIComponent(blockId)}/${mode}`;
  },

  meshSliceUrl(runId: string, blockId: string, axis: 'I' | 'J' | 'K', index: number): string {
    const query = new URLSearchParams({ block: blockId, axis, index: String(index) });
    return `${apiBase}/api/v1/runs/${encodeURIComponent(runId)}/mesh/slice?${query}`;
  },

  artifactUrl(artifactId: string): string {
    return `${apiBase}/api/v1/artifacts/${encodeURIComponent(artifactId)}`;
  },
};
