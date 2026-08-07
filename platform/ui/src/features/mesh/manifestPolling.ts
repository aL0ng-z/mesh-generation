import type { MeshManifest, RunSummary } from '../../api/types';

export const PREVIEW_POLL_INTERVAL_MS = 3_000;

/**
 * Worker 会先把网格任务写为 SUCCEEDED，再异步生成预览资产。
 * manifest 初次返回 UNAVAILABLE 时，以运行摘要的 PENDING 状态区分竞态与终态。
 */
export function manifestPollingInterval(
  run?: RunSummary,
  manifest?: MeshManifest,
): typeof PREVIEW_POLL_INTERVAL_MS | false {
  if (!run || run.status !== 'SUCCEEDED') return false;
  if (manifest?.status === 'READY') return false;
  if (run.preview_status === 'READY'
    || run.preview_status === 'UNAVAILABLE'
    || run.preview_status === 'FAILED') {
    return false;
  }
  if (manifest?.status === 'PENDING' || run.preview_status === 'PENDING') {
    return PREVIEW_POLL_INTERVAL_MS;
  }
  return false;
}

/** 预览终态变化进入新 Query key，确保在停止轮询前至少读取一次终态 manifest。 */
export function manifestQueryKey(run?: RunSummary) {
  return ['mesh-manifest', run?.id, run?.preview_status] as const;
}
