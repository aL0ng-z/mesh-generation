import { describe, expect, it } from 'vitest';
import type { MeshManifest, RunSummary } from '../../api/types';
import { manifestPollingInterval, manifestQueryKey } from './manifestPolling';

function run(previewStatus: RunSummary['preview_status']): RunSummary {
  return {
    id: 'run-1',
    session_id: 'session-1',
    sequence: 1,
    status: 'SUCCEEDED',
    quality_status: 'PASS',
    preview_status: previewStatus,
    created_at: '2026-08-07T00:00:00Z',
  };
}

function manifest(status: MeshManifest['status']): MeshManifest {
  return { status, blocks: [] };
}

describe('预览后处理轮询', () => {
  it('运行摘要仍为 PENDING 时重取初次 UNAVAILABLE manifest', () => {
    expect(manifestPollingInterval(run('PENDING'), manifest('UNAVAILABLE'))).toBe(3000);
  });

  it('manifest 自身为 PENDING 时继续轮询', () => {
    expect(manifestPollingInterval(run(undefined), manifest('PENDING'))).toBe(3000);
  });

  it('READY 或终态 UNAVAILABLE 后停止轮询', () => {
    expect(manifestPollingInterval(run('PENDING'), manifest('READY'))).toBe(false);
    expect(manifestPollingInterval(run('PENDING'), manifest('FAILED'))).toBe(false);
    expect(manifestPollingInterval(run('UNAVAILABLE'), manifest('UNAVAILABLE'))).toBe(false);
    expect(manifestPollingInterval(run('READY'), manifest('UNAVAILABLE'))).toBe(false);
    expect(manifestPollingInterval(run('UNAVAILABLE'), manifest('PENDING'))).toBe(false);
  });

  it('预览状态进入 Query key，终态变化会触发最后一次读取', () => {
    expect(manifestQueryKey(run('PENDING'))).not.toEqual(manifestQueryKey(run('READY')));
  });
});
