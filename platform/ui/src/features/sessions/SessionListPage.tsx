import { useMemo, useState } from 'react';
import { useInfiniteQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../../api/client';
import type { SessionStatus } from '../../api/types';
import { SystemHealthBanner } from '../health/SystemHealthBanner';
import { UploadDialog } from './UploadDialog';
import styles from './SessionListPage.module.css';

type Filter = 'ALL' | SessionStatus;

const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
});

function statusLabel(status: SessionStatus) {
  return status === 'COMPLETED' ? '已冻结' : '进行中';
}

export function SessionListPage() {
  const [params, setParams] = useSearchParams();
  const rawStatus = params.get('status');
  const filter: Filter = rawStatus === 'ACTIVE' || rawStatus === 'COMPLETED' ? rawStatus : 'ALL';
  const [uploadOpen, setUploadOpen] = useState(false);

  const sessionsQuery = useInfiniteQuery({
    queryKey: ['sessions', filter],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => api.listSessions(filter, pageParam),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });

  const sessions = useMemo(
    () => sessionsQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [sessionsQuery.data],
  );

  function changeFilter(next: Filter) {
    const nextParams = new URLSearchParams(params);
    if (next === 'ALL') nextParams.delete('status');
    else nextParams.set('status', next);
    setParams(nextParams, { replace: true });
  }

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <div>
          <p className={styles.eyebrow}>INTRANET · MESH EXPERIENCE</p>
          <h1>叶轮机械网格经验平台</h1>
          <p>以不可变运行树沉淀几何、控制、质量与专家经验。</p>
        </div>
        <button className={styles.primary} type="button" onClick={() => setUploadOpen(true)}>
          新建会话
        </button>
      </header>

      <section className={styles.workspace} aria-labelledby="sessions-heading">
        <SystemHealthBanner className={styles.healthBanner} />
        <div className={styles.toolbar}>
          <div>
            <p className={styles.kicker}>共享工作区</p>
            <h2 id="sessions-heading">网格会话</h2>
          </div>
          <div className={styles.filters} aria-label="会话状态筛选">
            {(
              [
                ['ALL', '全部'],
                ['ACTIVE', '进行中'],
                ['COMPLETED', '已冻结'],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                aria-pressed={filter === value}
                onClick={() => changeFilter(value)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {sessionsQuery.isPending ? <div className={styles.notice}>正在读取共享会话…</div> : null}
        {sessionsQuery.isError ? (
          <div className={styles.error} role="alert">
            <strong>会话列表加载失败</strong>
            <span>{(sessionsQuery.error as Error).message}</span>
            <button type="button" onClick={() => void sessionsQuery.refetch()}>
              重试
            </button>
          </div>
        ) : null}

        {!sessionsQuery.isPending && !sessionsQuery.isError && sessions.length === 0 ? (
          <div className={styles.empty}>
            <span aria-hidden="true">◎</span>
            <h3>这里还没有符合条件的会话</h3>
            <p>上传一个 .geomTurbo 文件，系统会自动创建 baseline。</p>
            <button className={styles.secondary} type="button" onClick={() => setUploadOpen(true)}>
              上传首个几何
            </button>
          </div>
        ) : null}

        <div className={styles.grid}>
          {sessions.map((session) => (
            <Link className={styles.card} key={session.id} to={`/sessions/${session.id}`}>
              <div className={styles.cardTop}>
                <span className={styles.fileType}>GT</span>
                <span className={styles[session.status === 'COMPLETED' ? 'frozen' : 'active']}>
                  {statusLabel(session.status)}
                </span>
              </div>
              <h3>{session.title}</h3>
              <p className={styles.filename}>{session.source_filename}</p>
              <dl>
                <div>
                  <dt>专家署名</dt>
                  <dd>{session.expert_signature || '未署名'}</dd>
                </div>
                <div>
                  <dt>最近更新</dt>
                  <dd>{dateFormatter.format(new Date(session.updated_at))}</dd>
                </div>
              </dl>
              <span className={styles.open}>进入工作台 →</span>
            </Link>
          ))}
        </div>

        {sessionsQuery.hasNextPage ? (
          <button
            className={styles.loadMore}
            type="button"
            disabled={sessionsQuery.isFetchingNextPage}
            onClick={() => void sessionsQuery.fetchNextPage()}
          >
            {sessionsQuery.isFetchingNextPage ? '正在加载…' : '加载更多'}
          </button>
        ) : null}
      </section>

      {uploadOpen ? <UploadDialog open onClose={() => setUploadOpen(false)} /> : null}
    </main>
  );
}
