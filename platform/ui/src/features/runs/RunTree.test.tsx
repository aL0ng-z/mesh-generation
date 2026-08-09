import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { RunSummary } from '../../api/types';
import { RunTree } from './RunTree';

it('将运行节点的归一化进度显示为百分比', () => {
  const run: RunSummary = {
    id: 'run-1',
    session_id: 'session-1',
    sequence: 1,
    status: 'RUNNING',
    quality_status: 'UNKNOWN',
    progress: 0.5,
    created_at: '2026-08-09T00:00:00Z',
  };

  render(
    <RunTree
      runs={[run]}
      frozen={false}
      onSelect={vi.fn()}
      onRetry={vi.fn()}
    />,
  );

  expect(screen.getByText('运行中 · 50%')).toBeInTheDocument();
});
