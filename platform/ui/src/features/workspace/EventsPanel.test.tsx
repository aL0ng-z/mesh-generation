import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import type { RunDetail } from '../../api/types';
import { EventsPanel } from './EventsPanel';

afterEach(() => vi.restoreAllMocks());

it('将归一化事件进度显示为百分比', async () => {
  vi.spyOn(api, 'getEvents').mockResolvedValue({
    items: [
      { sequence: 1, stage: '准备', level: 'INFO', progress: 0.01, message: '准备中', created_at: '2026-08-09T00:00:00Z' },
      { sequence: 2, stage: '生成', level: 'INFO', progress: 0.5, message: '生成中', created_at: '2026-08-09T00:00:01Z' },
      { sequence: 3, stage: '完成', level: 'INFO', progress: 1, message: '已完成', created_at: '2026-08-09T00:00:02Z' },
    ],
  });
  const run: RunDetail = {
    id: 'run-1',
    session_id: 'session-1',
    sequence: 1,
    status: 'SUCCEEDED',
    quality_status: 'PASS',
    created_at: '2026-08-09T00:00:00Z',
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <QueryClientProvider client={client}>
      <EventsPanel run={run} />
    </QueryClientProvider>,
  );

  expect(await screen.findByText('1%')).toBeInTheDocument();
  expect(screen.getByText('50%')).toBeInTheDocument();
  expect(screen.getByText('100%')).toBeInTheDocument();
});
