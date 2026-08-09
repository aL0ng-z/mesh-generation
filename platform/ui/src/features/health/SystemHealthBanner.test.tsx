import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import type { HealthSnapshot } from '../../api/types';
import { healthWarnings } from './healthStatus';
import { SystemHealthBanner } from './SystemHealthBanner';

function health(overrides: Partial<HealthSnapshot> = {}): HealthSnapshot {
  return {
    status: 'ok',
    database: { status: 'ok', version: 1 },
    worker: { online: true, id: 'worker-1', last_heartbeat: null, running_count: 0, max_concurrency: 20 },
    igg: { configured: true, available: true, path: null },
    queue: { queued: 0, running: 0, succeeded: 0, failed: 0 },
    resource_gate: {},
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it('健康状态不产生告警，未知降级原因提供兜底提示', () => {
  expect(healthWarnings(health())).toEqual([]);
  expect(healthWarnings(health({ status: 'degraded' }))).toEqual(['计算服务处于降级状态，请联系运维检查。']);
});

it('组合 Worker、IGG 与排队任务的可操作提示', () => {
  const warnings = healthWarnings(health({
    status: 'degraded',
    worker: { online: false, id: null, last_heartbeat: null, running_count: 0, max_concurrency: 20 },
    igg: { configured: false, available: false, path: null },
    queue: { queued: 2, running: 0, succeeded: 0, failed: 0 },
  }));
  expect(warnings).toEqual([
    'Worker 离线，排队任务暂时不会开始。',
    'IGG/AutoGrid 尚未配置，真实网格任务无法执行。',
    '当前有 2 个任务排队。',
  ]);
});

it('区分 IGG 已配置但路径不可用', () => {
  expect(healthWarnings(health({
    status: 'degraded',
    igg: { configured: true, available: false, path: 'C:\\NUMECA\\igg.exe' },
  }))).toEqual(['IGG/AutoGrid 配置路径不可用，真实网格任务无法执行。']);
});

it('在页面上展示降级状态', async () => {
  vi.spyOn(api, 'getHealth').mockResolvedValue(health({
    status: 'degraded',
    worker: { online: false, id: null, last_heartbeat: null, running_count: 0, max_concurrency: 20 },
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SystemHealthBanner />
    </QueryClientProvider>,
  );
  expect(await screen.findByRole('status')).toHaveTextContent('Worker 离线，排队任务暂时不会开始。');
});

it('健康请求失败时明确展示状态未知', async () => {
  vi.spyOn(api, 'getHealth').mockRejectedValue(new Error('network unavailable'));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SystemHealthBanner />
    </QueryClientProvider>,
  );
  const notice = await screen.findByRole('status');
  expect(notice).toHaveTextContent('求解服务状态未知');
  expect(notice).toHaveTextContent('无法获取求解服务状态，当前状态未知，请检查服务连接。');
});
