import { describe, expect, it } from 'vitest';
import type { SessionDetail } from '../../api/types';
import { isSessionFrozen, parseCompareIds, parseWorkspaceTab, pollingIntervalForSession } from './workspaceState';

function session(
  status: SessionDetail['status'],
  runStatus: SessionDetail['runs'][number]['status'],
  previewStatus?: SessionDetail['runs'][number]['preview_status'],
): SessionDetail {
  return {
    id: 'session', title: '测试', source_filename: 'a.geomTurbo', status,
    version: 1, created_at: '', updated_at: '',
    runs: [{
      id: 'run', session_id: 'session', sequence: 1, status: runStatus,
      quality_status: 'UNKNOWN', preview_status: previewStatus, created_at: '',
    }],
  };
}

describe('工作台状态', () => {
  it('仅活动运行每 3 秒轮询', () => {
    expect(pollingIntervalForSession(session('ACTIVE', 'RUNNING'))).toBe(3000);
    expect(pollingIntervalForSession(session('ACTIVE', 'QUEUED'))).toBe(3000);
    expect(pollingIntervalForSession(session('ACTIVE', 'SUCCEEDED'))).toBe(false);
    expect(pollingIntervalForSession(session('ACTIVE', 'SUCCEEDED', 'PENDING'))).toBe(3000);
    expect(pollingIntervalForSession(session('ACTIVE', 'SUCCEEDED', 'READY'))).toBe(false);
    expect(pollingIntervalForSession(session('ACTIVE', 'SUCCEEDED', 'UNAVAILABLE'))).toBe(false);
  });

  it('冻结状态来自会话而非质量结果', () => {
    expect(isSessionFrozen(session('COMPLETED', 'SUCCEEDED'))).toBe(true);
    expect(isSessionFrozen(session('ACTIVE', 'FAILED'))).toBe(false);
  });

  it('显式后处理状态优先：失败任务仍轮询至后处理终结', () => {
    const value = session('ACTIVE', 'FAILED', 'UNAVAILABLE');
    value.runs[0].postprocess_status = 'PENDING';
    expect(pollingIntervalForSession(value)).toBe(3000);
    value.runs[0].postprocess_status = 'RUNNING';
    expect(pollingIntervalForSession(value)).toBe(3000);
    value.runs[0].postprocess_status = 'FAILED';
    value.runs[0].preview_status = 'PENDING';
    expect(pollingIntervalForSession(value)).toBe(false);
    value.runs[0].postprocess_status = 'COMPLETED';
    expect(pollingIntervalForSession(value)).toBe(false);
  });

  it('只恢复有效页签和两个不同的成功运行', () => {
    expect(parseWorkspaceTab('bad')).toBe('viewer');
    expect(parseWorkspaceTab('events')).toBe('events');
    const successful = new Set(['a', 'b']);
    expect(parseCompareIds('a,b', successful)).toEqual(['a', 'b']);
    expect(parseCompareIds('a,a', successful)).toBeUndefined();
    expect(parseCompareIds('a,c', successful)).toBeUndefined();
  });
});
