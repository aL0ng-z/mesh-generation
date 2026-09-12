import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { QualityPanel } from './QualityPanel';

afterEach(cleanup);

it('单轮质量显示科学计数法保留微小非零体积', () => {
  render(<QualityPanel run={{
    id: 'run', session_id: 'session', sequence: 1, status: 'SUCCEEDED', quality_status: 'PASS', created_at: '',
    quality: { metrics: { min_volume: 5.2e-12, negative_cells: 0 } },
  }} />);
  expect(screen.getByText('5.2E-12')).toBeInTheDocument();
  expect(screen.getByText('0')).toBeInTheDocument();
});
