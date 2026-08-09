import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { RunDetail } from '../../api/types';
import { isActiveRun } from '../runs/runTreeModel';
import { progressPercent } from '../runs/progress';
import styles from './Panels.module.css';

export function EventsPanel({ run }: { run?: RunDetail }) {
  const eventQuery = useQuery({
    queryKey: ['run-events', run?.id],
    queryFn: () => api.getEvents(run!.id),
    enabled: Boolean(run?.id),
    refetchInterval: run && isActiveRun(run) ? 3_000 : false,
  });
  const events = eventQuery.data?.items ?? run?.events ?? [];
  if (!run) return <div className={styles.empty}>请选择运行节点。</div>;
  return (
    <section className={styles.panel} aria-labelledby="events-title">
      <header>
        <div>
          <p>INCREMENTAL EVENTS</p>
          <h2 id="events-title">运行活动</h2>
        </div>
        {isActiveRun(run) ? <span className={styles.live}>每 3 秒刷新</span> : null}
      </header>
      {eventQuery.isError ? <p className={styles.error}>{(eventQuery.error as Error).message}</p> : null}
      {!events.length ? <div className={styles.empty}>暂无事件。</div> : (
        <ol className={styles.timeline}>
          {[...events].sort((a, b) => b.sequence - a.sequence).map((event) => (
            <li key={event.sequence} data-level={event.level}>
              <span>{event.stage}</span>
              <div>
                <p>{event.message}</p>
                <time>{new Date(event.created_at).toLocaleString('zh-CN')}</time>
              </div>
              {event.progress != null ? <strong>{progressPercent(event.progress)}%</strong> : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
