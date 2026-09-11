import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import type { RunDetail } from '../../api/types';
import { RunFacts } from './RunFacts';

afterEach(() => cleanup());

function makeRun(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    id: 'run-1',
    session_id: 'session-1',
    sequence: 1,
    status: 'SUCCEEDED',
    quality_status: 'PASS',
    created_at: '2026-08-09T00:00:00Z',
    ...overrides,
  };
}

it('展示合格的样本资格与已完成的后处理状态', () => {
  render(<RunFacts run={makeRun({
    sample_eligibility: { eligible: true, reasons: [] },
    postprocess_status: 'COMPLETED',
  })} />);
  expect(screen.getByText('样本资格：合格')).toBeInTheDocument();
  expect(screen.getByText('后处理：已完成')).toBeInTheDocument();
});

it('不合格样本展示原因提示，后处理失败展示错误提示', () => {
  render(<RunFacts run={makeRun({
    sample_eligibility: { eligible: false, reasons: ['新证据缺失', '产物未全部登记为平台产物'] },
    postprocess_status: 'FAILED',
    postprocess_error: '后处理超时：超过 600 秒，进程树已终止。',
  })} />);
  expect(screen.getByText('样本资格：未合格').getAttribute('title'))
    .toBe('新证据缺失；产物未全部登记为平台产物');
  expect(screen.getByText('后处理：处理失败').getAttribute('title'))
    .toBe('后处理超时：超过 600 秒，进程树已终止。');
});

it('非终态运行不展示资格与后处理信息', () => {
  const { container } = render(<RunFacts run={makeRun({ status: 'RUNNING' })} />);
  expect(container.firstChild).toBeNull();
});
