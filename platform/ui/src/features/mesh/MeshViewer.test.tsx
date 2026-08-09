import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import type { MeshManifest, RunDetail } from '../../api/types';
import { MeshViewer } from './MeshViewer';

vi.mock('./MeshCanvas', () => ({ MeshCanvas: () => <div data-testid="mesh-canvas" /> }));

afterEach(() => vi.restoreAllMocks());

it('为 I/J/K 切片的网格块和轴向下拉框提供可访问名称', async () => {
  const manifest: MeshManifest = {
    status: 'READY',
    blocks: [{
      id: 'b0001',
      name: 'domain1',
      dimensions: [69, 73, 81],
      surface: true,
      wireframe: true,
      slices: { I: { minimum: 0, maximum: 68 } },
    }],
    capabilities: { slice: true, slice_index_base: 0 },
  };
  vi.spyOn(api, 'getMeshManifest').mockResolvedValue(manifest);
  const run: RunDetail = {
    id: 'run-1',
    session_id: 'session-1',
    sequence: 1,
    status: 'SUCCEEDED',
    quality_status: 'PASS',
    preview_status: 'READY',
    created_at: '2026-08-09T00:00:00Z',
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <QueryClientProvider client={client}>
      <MeshViewer run={run} />
    </QueryClientProvider>,
  );

  fireEvent.click(await screen.findByRole('checkbox', { name: 'I/J/K 切片' }));
  expect(screen.getByRole('combobox', { name: '切片网格块' })).toBeInTheDocument();
  expect(screen.getByRole('combobox', { name: '切片轴向' })).toBeInTheDocument();
});
