import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { RunSummary } from '../../api/types';
import { MeshViewer } from '../mesh/MeshViewer';
import { isActiveRun } from '../runs/runTreeModel';
import { calculateQualityDeltas, formatQualityNumber } from './qualityDelta';
import styles from './ComparePanel.module.css';

interface Props {
  runIds: [string, string];
  runs: readonly RunSummary[];
}

export function ComparePanel({ runIds, runs }: Props) {
  const leftQuery = useQuery({
    queryKey: ['run', runIds[0]],
    queryFn: () => api.getRun(runIds[0]),
    refetchInterval: (query) => query.state.data && isActiveRun(query.state.data) ? 3_000 : false,
  });
  const rightQuery = useQuery({
    queryKey: ['run', runIds[1]],
    queryFn: () => api.getRun(runIds[1]),
    refetchInterval: (query) => query.state.data && isActiveRun(query.state.data) ? 3_000 : false,
  });
  const left = leftQuery.data;
  const right = rightQuery.data;
  const leftSummary = runs.find((run) => run.id === runIds[0]);
  const rightSummary = runs.find((run) => run.id === runIds[1]);
  const deltas = calculateQualityDeltas(left?.quality, right?.quality);

  if (leftQuery.isPending || rightQuery.isPending) return <div className={styles.notice}>正在加载双轮对比…</div>;
  if (leftQuery.isError || rightQuery.isError) {
    return <div className={styles.notice}>对比数据加载失败：{(leftQuery.error as Error | null)?.message ?? (rightQuery.error as Error).message}</div>;
  }
  return (
    <section className={styles.compare} aria-label="双轮网格对比">
      <div className={styles.viewers}>
        <article>
          <header><span>A</span><strong>{leftSummary?.label || `运行 #${leftSummary?.sequence ?? '?'}`}</strong><small>{left?.quality_status}</small></header>
          <MeshViewer key={left?.id} run={left} compact cameraGroup={`compare:${runIds.join(':')}`} />
        </article>
        <article>
          <header><span>B</span><strong>{rightSummary?.label || `运行 #${rightSummary?.sequence ?? '?'}`}</strong><small>{right?.quality_status}</small></header>
          <MeshViewer key={right?.id} run={right} compact cameraGroup={`compare:${runIds.join(':')}`} />
        </article>
      </div>
      <section className={styles.deltas} aria-labelledby="delta-title">
        <header>
          <div><p>CLIENT-SIDE DELTA</p><h2 id="delta-title">质量差值（B − A）</h2></div>
          <span>{deltas.length} 项共有指标</span>
        </header>
        {!deltas.length ? <p className={styles.empty}>两轮暂无可共同计算的数值质量指标。</p> : (
          <div className={styles.tableWrap}>
            <table>
              <thead><tr><th>指标</th><th>A</th><th>B</th><th>差值</th></tr></thead>
              <tbody>
                {deltas.map((delta) => {
                  const normalizedDelta = Object.is(delta.delta, -0) ? 0 : delta.delta;
                  return (
                    <tr key={delta.key}>
                      <th>{delta.label}</th>
                      <td>{formatQualityNumber(delta.left)}{delta.unit}</td>
                      <td>{formatQualityNumber(delta.right)}{delta.unit}</td>
                      <td data-sign={normalizedDelta > 0 ? 'positive' : normalizedDelta < 0 ? 'negative' : 'zero'}>
                        {normalizedDelta > 0 ? '+' : ''}{formatQualityNumber(normalizedDelta)}{delta.unit}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </section>
  );
}
