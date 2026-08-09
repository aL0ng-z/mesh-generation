import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import type { RunDetail } from '../../api/types';
import { ArtifactsPanel } from './ArtifactsPanel';

it('使用 block 标识区分同名预览产物', () => {
  const run: RunDetail = {
    id: 'run-1',
    session_id: 'session-1',
    sequence: 1,
    status: 'SUCCEEDED',
    quality_status: 'PASS',
    created_at: '2026-08-09T00:00:00Z',
    artifacts: [
      { id: 'a-1', type: 'PREVIEW_SURFACE', display_name: 'surface.vtp', block_id: 'b0001', size: 10 },
      { id: 'a-2', type: 'PREVIEW_SURFACE', display_name: 'surface.vtp', block_id: 'b0002', size: 20 },
    ],
  };

  render(<ArtifactsPanel run={run} />);
  expect(screen.getByText('b0001 / surface.vtp')).toBeInTheDocument();
  expect(screen.getByText('b0002 / surface.vtp')).toBeInTheDocument();
  expect(screen.getAllByRole('link', { name: '下载' }).map((link) => link.getAttribute('href'))).toEqual([
    '/api/v1/artifacts/a-1',
    '/api/v1/artifacts/a-2',
  ]);
});
