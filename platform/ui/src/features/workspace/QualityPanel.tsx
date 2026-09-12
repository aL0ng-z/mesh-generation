import type { QualityMetric, RunDetail } from '../../api/types';
import { formatQualityNumber, normalizeQualityMetrics, qualityReportStatus } from '../compare/qualityDelta';
import styles from './Panels.module.css';

function formatValue(metric: QualityMetric) {
  if (metric.value == null || metric.value === '') return '—';
  if (typeof metric.value === 'number') return formatQualityNumber(metric.value);
  return metric.value;
}

export function QualityPanel({ run }: { run?: RunDetail }) {
  if (!run) return <div className={styles.empty}>请选择运行节点。</div>;
  const report = run.quality;
  const metrics = normalizeQualityMetrics(report);
  const status = qualityReportStatus(report, run.quality_status);
  return (
    <section className={styles.panel} aria-labelledby="quality-title">
      <header>
        <div>
          <p>QUALITY REPORT</p>
          <h2 id="quality-title">网格质量</h2>
        </div>
        <span className={styles[`status${status}`]}>{status}</span>
      </header>
      {report?.summary ? <p className={styles.summary}>{report.summary}</p> : null}
      {report?.result?.reasons?.length ? <p className={styles.summary}>{report.result.reasons.join('；')}</p> : null}
      {!metrics.length ? (
        <div className={styles.empty}>当前运行尚无结构化质量指标。质量 UNKNOWN/FAIL 不改变任务成功状态。</div>
      ) : (
        <div className={styles.metricGrid}>
          {metrics.map((metric) => (
            <article key={metric.key} data-status={metric.status ?? 'UNKNOWN'}>
              <span>{metric.label ?? metric.key}</span>
              <strong>{formatValue(metric)}{metric.unit ? <small>{metric.unit}</small> : null}</strong>
              {metric.limit != null ? <small>准则 {String(metric.limit)}</small> : null}
              {metric.message ? <p>{metric.message}</p> : null}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
