import type { PostprocessStatus, RunDetail } from '../../api/types';
import styles from './SessionWorkspacePage.module.css';

const postprocessLabels: Record<PostprocessStatus, string> = {
  PENDING: '待处理',
  RUNNING: '处理中',
  COMPLETED: '已完成',
  FAILED: '处理失败',
};

export function RunFacts({ run }: { run?: RunDetail }) {
  if (!run || (run.status !== 'SUCCEEDED' && run.status !== 'FAILED')) return null;
  const eligibility = run.sample_eligibility;
  const eligible = Boolean(eligibility?.eligible);
  const postprocess = run.postprocess_status ?? 'PENDING';
  return (
    <div className={styles.runFacts} role="note">
      <span
        data-eligible={eligible ? 'true' : 'false'}
        title={!eligible && eligibility?.reasons.length ? eligibility.reasons.join('；') : undefined}
      >
        样本资格：{eligible ? '合格' : '未合格'}
      </span>
      <span title={postprocess === 'FAILED' ? run.postprocess_error ?? undefined : undefined}>
        后处理：{postprocessLabels[postprocess]}
      </span>
    </div>
  );
}
