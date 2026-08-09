import { api } from '../../api/client';
import type { RunDetail } from '../../api/types';
import styles from './Panels.module.css';

function bytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(2)} GiB`;
}

export function ArtifactsPanel({ run }: { run?: RunDetail }) {
  if (!run) return <div className={styles.empty}>请选择运行节点。</div>;
  const artifacts = run.artifacts ?? [];
  return (
    <section className={styles.panel} aria-labelledby="artifacts-title">
      <header>
        <div>
          <p>CONTROLLED DOWNLOAD</p>
          <h2 id="artifacts-title">运行产物</h2>
        </div>
        <span>{artifacts.length} 个文件</span>
      </header>
      {!artifacts.length ? <div className={styles.empty}>暂无可下载产物。</div> : (
        <div className={styles.artifacts}>
          {artifacts.map((artifact) => (
            <article key={artifact.id}>
              <span>{artifact.type}</span>
              <div>
                <strong>{artifact.block_id ? `${artifact.block_id} / ` : ''}{artifact.display_name}</strong>
                <small>{bytes(artifact.size)}{artifact.sha256 ? ` · SHA-256 ${artifact.sha256.slice(0, 12)}…` : ''}</small>
              </div>
              <a href={api.artifactUrl(artifact.id)}>下载</a>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
