import type { RunSummary } from '../../api/types';

export interface RunTreeNode {
  run: RunSummary;
  children: RunTreeNode[];
}

/** 从扁平响应构建新树，不修改 Query 缓存中的运行对象或数组。 */
export function buildRunForest(runs: readonly RunSummary[]): RunTreeNode[] {
  const ordered = [...runs].sort((left, right) => left.sequence - right.sequence);
  const nodeById = new Map<string, RunTreeNode>();
  for (const run of ordered) nodeById.set(run.id, { run, children: [] });

  const roots: RunTreeNode[] = [];
  for (const run of ordered) {
    const node = nodeById.get(run.id)!;
    const parent = run.parent_run_id ? nodeById.get(run.parent_run_id) : undefined;
    if (parent && parent !== node) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

export function isActiveRun(run: RunSummary): boolean {
  return run.status === 'QUEUED'
    || run.status === 'RUNNING'
    || run.postprocess_status === 'PENDING'
    || run.postprocess_status === 'RUNNING'
    || (run.postprocess_status === undefined && run.status === 'SUCCEEDED' && run.preview_status === 'PENDING');
}

export function latestRunId(runs: readonly RunSummary[]): string | undefined {
  return [...runs].sort((left, right) => right.sequence - left.sequence)[0]?.id;
}
