import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { ApiError, api } from '../../api/client';
import type { RunDetail, SessionDetail } from '../../api/types';
import { SessionWorkspacePage } from './SessionWorkspacePage';

vi.mock('../mesh/MeshViewer', () => ({ MeshViewer: () => null }));
vi.mock('../compare/ComparePanel', () => ({ ComparePanel: () => null }));
vi.mock('../health/SystemHealthBanner', () => ({ SystemHealthBanner: () => null }));
vi.mock('../controls/ControlEditor', () => ({ ControlEditor: () => null }));

const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); vi.restoreAllMocks(); });

const finished: RunDetail = {
  id: 'run-1', session_id: 'session-1', sequence: 1, status: 'SUCCEEDED',
  quality_status: 'PASS', preview_status: 'UNAVAILABLE', postprocess_status: 'COMPLETED',
  created_at: '', experience_note: '已保存经验', note_version: 1,
  quality: { metrics: { min_volume: 5.2e-12 } },
};
function session(run: RunDetail): SessionDetail {
  return { id: 'session-1', title: '测试会话', source_filename: 'Rotor37.geomTurbo', status: 'ACTIVE',
    version: 1, created_at: '', updated_at: '', runs: [run] };
}
function renderWorkspace(tab = 'quality') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } });
  clients.push(client);
  const router = createMemoryRouter([{ path: '/sessions/:id', element: <SessionWorkspacePage /> }], {
    initialEntries: [`/sessions/session-1?run=run-1&tab=${tab}`],
  });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return client;
}

it('摘要先进入终态时补取详情，详情仍活动时自行继续轮询到最终质量', async () => {
  const active: RunDetail = { ...finished, status: 'RUNNING', quality: null, postprocess_status: 'PENDING' };
  vi.spyOn(api, 'getSession').mockResolvedValue(session(active));
  const getRun = vi.spyOn(api, 'getRun').mockResolvedValueOnce(active).mockResolvedValueOnce(active).mockResolvedValue(finished);
  const client = renderWorkspace();
  await screen.findByText('当前运行尚无结构化质量指标。质量 UNKNOWN/FAIL 不改变任务成功状态。');
  act(() => { client.setQueryData(['session', 'session-1'], session(finished)); });
  await waitFor(() => expect(getRun).toHaveBeenCalledTimes(2));
  expect(await screen.findByText('5.2E-12', {}, { timeout: 4500 })).toBeInTheDocument();
  expect(getRun).toHaveBeenCalledTimes(3);
});

it('手动刷新同步更新摘要、详情和当前活动面板', async () => {
  const getSession = vi.spyOn(api, 'getSession').mockResolvedValue(session(finished));
  const getRun = vi.spyOn(api, 'getRun').mockResolvedValue(finished);
  const getEvents = vi.spyOn(api, 'getEvents').mockResolvedValue({ items: [] });
  renderWorkspace('events');
  await screen.findByText('暂无事件。');
  await waitFor(() => expect(screen.getByRole('button', { name: '刷新' })).toBeEnabled());
  const runCalls = getRun.mock.calls.length;
  const sessionCalls = getSession.mock.calls.length;
  getEvents.mockResolvedValue({ items: [{ sequence: 1, stage: 'postprocess', level: 'INFO', message: '后处理日志已刷新', created_at: '' }] });
  fireEvent.click(screen.getByRole('button', { name: '刷新' }));
  expect(await screen.findByText('后处理日志已刷新')).toBeInTheDocument();
  expect(getRun.mock.calls.length).toBeGreaterThan(runCalls);
  expect(getSession.mock.calls.length).toBeGreaterThan(sessionCalls);
});

it('失败任务后处理进入终态时补取最终诊断事件', async () => {
  const failed: RunDetail = { ...finished, status: 'FAILED', postprocess_status: 'RUNNING' };
  vi.spyOn(api, 'getSession').mockResolvedValue(session(failed));
  vi.spyOn(api, 'getRun').mockResolvedValue(failed);
  const getEvents = vi.spyOn(api, 'getEvents').mockResolvedValue({ items: [] });
  const client = renderWorkspace('events');
  await screen.findByText('每 3 秒刷新');
  getEvents.mockResolvedValue({ items: [{ sequence: 1, stage: 'postprocess', level: 'ERROR', message: '最终诊断日志', created_at: '' }] });
  act(() => { client.setQueryData(['run', 'run-1'], { ...failed, postprocess_status: 'COMPLETED' }); });
  expect(await screen.findByText('最终诊断日志')).toBeInTheDocument();
  expect(screen.queryByText('每 3 秒刷新')).not.toBeInTheDocument();
});

it('会话后台刷新发生 401 时保留已有工作台和经验草稿组件', async () => {
  const getSession = vi.spyOn(api, 'getSession').mockResolvedValue(session(finished));
  vi.spyOn(api, 'getRun').mockResolvedValue(finished);
  const client = renderWorkspace();
  const textarea = await screen.findByRole('textbox');
  fireEvent.change(textarea, { target: { value: '未保存的经验' } });
  getSession.mockRejectedValue(new ApiError(401, {}));
  await act(async () => { await client.invalidateQueries({ queryKey: ['session', 'session-1'] }); });
  expect(client.getQueryState(['session', 'session-1'])?.status).toBe('error');
  expect(screen.getByRole('textbox')).toBe(textarea);
  expect(textarea).toHaveValue('未保存的经验');
});
