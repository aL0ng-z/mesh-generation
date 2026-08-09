import { useMemo } from 'react';
import type { RunSummary } from '../../api/types';
import { progressPercent } from './progress';
import { buildRunForest, type RunTreeNode } from './runTreeModel';
import styles from './RunTree.module.css';

interface Props {
  runs: readonly RunSummary[];
  selectedRunId?: string;
  frozen: boolean;
  retryingRunId?: string;
  onSelect: (runId: string) => void;
  onRetry: (runId: string) => void;
}

const statuses: Record<RunSummary['status'], string> = {
  QUEUED: '排队',
  RUNNING: '运行中',
  SUCCEEDED: '成功',
  FAILED: '失败',
};

function Node({
  node,
  selectedRunId,
  frozen,
  retryingRunId,
  onSelect,
  onRetry,
}: Omit<Props, 'runs'> & { node: RunTreeNode }) {
  const { run } = node;
  const baseline = !run.parent_run_id && !run.retry_of_run_id;
  return (
    <li>
      <div className={styles.row} data-selected={selectedRunId === run.id}>
        <button className={styles.select} type="button" onClick={() => onSelect(run.id)}>
          <span className={styles.branchMark} data-status={run.status} aria-hidden="true" />
          <span className={styles.nodeText}>
            <strong>{run.label || (baseline ? 'baseline' : `运行 #${run.sequence}`)}</strong>
            <small>
              {run.status === 'SUCCEEDED' && run.preview_status === 'PENDING'
                ? '成功 · 预览处理中'
                : statuses[run.status]}
              {run.retry_of_run_id ? ' · 重试' : ''}
              {run.progress != null && run.status === 'RUNNING' ? ` · ${progressPercent(run.progress)}%` : ''}
            </small>
          </span>
          <span className={styles.immutable} title="运行节点不可变">◇</span>
        </button>
        {run.status === 'FAILED' && !frozen ? (
          <button
            className={styles.retry}
            type="button"
            disabled={retryingRunId === run.id}
            onClick={() => onRetry(run.id)}
          >
            {retryingRunId === run.id ? '提交中' : '重试'}
          </button>
        ) : null}
      </div>
      {node.children.length ? (
        <ul>
          {node.children.map((child) => (
            <Node
              key={child.run.id}
              node={child}
              selectedRunId={selectedRunId}
              frozen={frozen}
              retryingRunId={retryingRunId}
              onSelect={onSelect}
              onRetry={onRetry}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export function RunTree(props: Props) {
  const forest = useMemo(() => buildRunForest(props.runs), [props.runs]);
  return (
    <nav className={styles.tree} aria-label="不可变运行树">
      <div className={styles.heading}>
        <span>运行谱系</span>
        <small>{props.runs.length} 个节点</small>
      </div>
      {forest.length ? (
        <ul className={styles.roots}>
          {forest.map((node) => <Node key={node.run.id} node={node} {...props} />)}
        </ul>
      ) : (
        <p className={styles.empty}>等待 baseline 创建…</p>
      )}
    </nav>
  );
}
