import { useCallback, useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useBeforeUnload, useBlocker, useParams, useSearchParams } from 'react-router-dom';
import { api, newRequestId } from '../../api/client';
import type { RunSummary } from '../../api/types';
import { ComparePanel } from '../compare/ComparePanel';
import { ControlEditor } from '../controls/ControlEditor';
import { MeshViewer } from '../mesh/MeshViewer';
import { ExperienceNote } from '../runs/ExperienceNote';
import { RunTree } from '../runs/RunTree';
import { isActiveRun, latestRunId } from '../runs/runTreeModel';
import { ArtifactsPanel } from './ArtifactsPanel';
import { EventsPanel } from './EventsPanel';
import { QualityPanel } from './QualityPanel';
import {
  isSessionFrozen,
  parseCompareIds,
  parseWorkspaceTab,
  pollingIntervalForSession,
  type WorkspaceTab,
} from './workspaceState';
import styles from './SessionWorkspacePage.module.css';

const tabLabels: Record<WorkspaceTab, string> = {
  viewer: '网格 Viewer',
  quality: '质量',
  events: '活动',
  artifacts: '产物',
};

const runStatusLabels: Record<RunSummary['status'], string> = {
  QUEUED: '排队中',
  RUNNING: '运行中',
  SUCCEEDED: '运行成功',
  FAILED: '运行失败',
};

function CompareControls({
  successfulRuns,
  selectedRunId,
  active,
  onCompare,
  onExit,
}: {
  successfulRuns: RunSummary[];
  selectedRunId?: string;
  active?: [string, string];
  onCompare: (ids: [string, string]) => void;
  onExit: () => void;
}) {
  const firstDefault = active?.[0] ?? (successfulRuns.some((run) => run.id === selectedRunId) ? selectedRunId : successfulRuns[0]?.id) ?? '';
  const secondDefault = active?.[1] ?? successfulRuns.find((run) => run.id !== firstDefault)?.id ?? '';
  const [left, setLeft] = useState(firstDefault);
  const [right, setRight] = useState(secondDefault);
  return (
    <div className={styles.compareControls}>
      <span>对比</span>
      <select aria-label="对比运行 A" value={left} onChange={(event) => setLeft(event.target.value)}>
        <option value="">选择 A</option>
        {successfulRuns.map((run) => <option key={run.id} value={run.id}>#{run.sequence} {run.label || run.id.slice(0, 6)}</option>)}
      </select>
      <span>↔</span>
      <select aria-label="对比运行 B" value={right} onChange={(event) => setRight(event.target.value)}>
        <option value="">选择 B</option>
        {successfulRuns.map((run) => <option key={run.id} value={run.id}>#{run.sequence} {run.label || run.id.slice(0, 6)}</option>)}
      </select>
      {active ? <button type="button" onClick={onExit}>退出</button> : (
        <button type="button" disabled={!left || !right || left === right} onClick={() => onCompare([left, right])}>开始</button>
      )}
    </div>
  );
}

export function SessionWorkspacePage() {
  const { id: sessionId = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const queryClient = useQueryClient();
  const [controlDirty, setControlDirty] = useState(false);
  const [noteDirty, setNoteDirty] = useState(false);
  const dirty = controlDirty || noteDirty;

  const sessionQuery = useQuery({
    queryKey: ['session', sessionId],
    queryFn: () => api.getSession(sessionId),
    enabled: Boolean(sessionId),
    refetchInterval: (query) => pollingIntervalForSession(query.state.data),
  });
  const session = sessionQuery.data;
  const selectedParam = params.get('run') ?? undefined;
  const selectedRunId = session?.runs.some((run) => run.id === selectedParam)
    ? selectedParam
    : session?.satisfied_run_id ?? latestRunId(session?.runs ?? []);
  const selectedSummary = session?.runs.find((run) => run.id === selectedRunId);
  const activeTab = parseWorkspaceTab(params.get('tab'));
  const successfulRuns = useMemo(() => session?.runs.filter((run) => run.status === 'SUCCEEDED') ?? [], [session?.runs]);
  const successfulIds = useMemo(() => new Set(successfulRuns.map((run) => run.id)), [successfulRuns]);
  const compareIds = parseCompareIds(params.get('compare'), successfulIds);
  const frozen = isSessionFrozen(session);

  const runQuery = useQuery({
    queryKey: ['run', selectedRunId],
    queryFn: () => api.getRun(selectedRunId!),
    enabled: Boolean(selectedRunId),
    refetchInterval: selectedSummary && isActiveRun(selectedSummary) ? 3_000 : false,
  });

  useEffect(() => {
    if (!selectedRunId || params.get('run')) return;
    const next = new URLSearchParams(params);
    next.set('run', selectedRunId);
    setParams(next, { replace: true });
  }, [params, selectedRunId, setParams]);

  useBeforeUnload(useCallback((event) => {
    if (!dirty) return;
    event.preventDefault();
  }, [dirty]));
  const blocker = useBlocker(dirty);
  useEffect(() => {
    if (blocker.state !== 'blocked') return;
    if (window.confirm('存在未保存的控制草稿或经验文本。确定离开并放弃这些更改吗？')) blocker.proceed();
    else blocker.reset();
  }, [blocker]);

  const retryMutation = useMutation({
    mutationFn: (runId: string) => api.retryRun(runId, session!.version, newRequestId()),
    onSuccess: async (run) => {
      await queryClient.invalidateQueries({ queryKey: ['session', sessionId] });
      selectRun(run.id);
    },
  });

  const completeMutation = useMutation({
    mutationFn: () => api.completeSession(sessionId, selectedRunId!, session!.version),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['session', sessionId] });
      await queryClient.invalidateQueries({ queryKey: ['run', selectedRunId] });
    },
  });

  function updateParams(update: (next: URLSearchParams) => void) {
    const next = new URLSearchParams(params);
    update(next);
    setParams(next, { replace: true });
  }

  function selectRun(runId: string) {
    updateParams((next) => {
      next.set('run', runId);
      next.delete('compare');
    });
  }

  function selectTab(tab: WorkspaceTab) {
    updateParams((next) => {
      next.set('tab', tab);
      next.delete('compare');
    });
  }

  function freeze() {
    if (!selectedSummary || selectedSummary.status !== 'SUCCEEDED' || frozen) return;
    const accepted = window.confirm(
      `确定选择“${selectedSummary.label || `运行 #${selectedSummary.sequence}`}”作为满意网格并冻结会话吗？\n\n冻结后不能再创建分支、重试或编辑经验文本。`,
    );
    if (accepted) completeMutation.mutate();
  }

  const onControlDirty = useCallback((value: boolean) => setControlDirty(value), []);
  const onNoteDirty = useCallback((value: boolean) => setNoteDirty(value), []);

  if (sessionQuery.isPending) return <main className={styles.statePage}>正在加载专家工作台…</main>;
  if (sessionQuery.isError || !session) {
    return (
      <main className={styles.statePage}>
        <strong>无法打开会话</strong>
        <p>{(sessionQuery.error as Error | null)?.message ?? '会话不存在。'}</p>
        <Link to="/">返回共享工作区</Link>
      </main>
    );
  }

  return (
    <main className={styles.workspace}>
      <header className={styles.topbar}>
        <Link className={styles.back} to="/" aria-label="返回会话列表">←</Link>
        <div className={styles.identity}>
          <p>{session.source_filename}</p>
          <h1>{session.title}</h1>
        </div>
        {selectedSummary ? (
          <div className={styles.runState} data-status={selectedSummary.status}>
            <span aria-hidden="true" />
            <div>
              <small>当前节点</small>
              <strong>
                {selectedSummary.status === 'SUCCEEDED' && selectedSummary.preview_status === 'PENDING'
                  ? '运行成功 · 预览处理中'
                  : runStatusLabels[selectedSummary.status]}
              </strong>
            </div>
          </div>
        ) : null}
        {frozen ? <span className={styles.frozenBadge}>已冻结 · #{session.runs.find((run) => run.id === session.satisfied_run_id)?.sequence ?? '?'}</span> : null}
        <CompareControls
          key={compareIds?.join(',') ?? `${selectedRunId}:${successfulRuns.length}`}
          successfulRuns={successfulRuns}
          selectedRunId={selectedRunId}
          active={compareIds}
          onCompare={(ids) => updateParams((next) => next.set('compare', ids.join(',')))}
          onExit={() => updateParams((next) => next.delete('compare'))}
        />
        <button className={styles.refresh} type="button" disabled={sessionQuery.isFetching} onClick={() => void sessionQuery.refetch()}>
          {sessionQuery.isFetching ? '刷新中…' : '刷新'}
        </button>
        <button
          className={styles.freeze}
          type="button"
          disabled={frozen || selectedSummary?.status !== 'SUCCEEDED' || completeMutation.isPending || dirty}
          title={dirty ? '请先保存或放弃草稿' : undefined}
          onClick={freeze}
        >
          {frozen ? '会话已冻结' : completeMutation.isPending ? '冻结中…' : '选为满意网格并冻结'}
        </button>
      </header>

      {retryMutation.isError || completeMutation.isError ? (
        <div className={styles.banner} role="alert">{(retryMutation.error as Error | null)?.message ?? (completeMutation.error as Error).message}</div>
      ) : null}

      <div className={styles.columns}>
        <aside className={styles.left}>
          <RunTree
            runs={session.runs}
            selectedRunId={selectedRunId}
            frozen={frozen}
            retryingRunId={retryMutation.isPending ? retryMutation.variables : undefined}
            onSelect={selectRun}
            onRetry={(runId) => retryMutation.mutate(runId)}
          />
          <footer>
            <small>不可变谱系</small>
            <span>成功或失败节点均只读；分支与重试创建新节点。</span>
          </footer>
        </aside>

        <section className={styles.center}>
          <nav className={styles.tabs} aria-label="运行详情页签">
            {(Object.keys(tabLabels) as WorkspaceTab[]).map((tab) => (
              <button key={tab} type="button" aria-current={!compareIds && activeTab === tab ? 'page' : undefined} onClick={() => selectTab(tab)}>
                {tabLabels[tab]}
              </button>
            ))}
            {compareIds ? <span>双轮同步对比</span> : null}
          </nav>
          <div className={styles.tabContent} role="tabpanel">
            {compareIds ? <ComparePanel runIds={compareIds} runs={session.runs} /> : null}
            {!compareIds && activeTab === 'viewer' ? <MeshViewer key={runQuery.data?.id} run={runQuery.data} /> : null}
            {!compareIds && activeTab === 'quality' ? <QualityPanel run={runQuery.data} /> : null}
            {!compareIds && activeTab === 'events' ? <EventsPanel run={runQuery.data} /> : null}
            {!compareIds && activeTab === 'artifacts' ? <ArtifactsPanel run={runQuery.data} /> : null}
            {!compareIds && runQuery.isPending ? <div className={styles.loading}>正在读取运行详情…</div> : null}
            {!compareIds && runQuery.isError ? <div className={styles.loading}>{(runQuery.error as Error).message}</div> : null}
          </div>
        </section>

        <aside className={styles.right}>
          <div className={styles.controlsPane}>
            <ControlEditor
              key={selectedSummary?.id ?? 'no-run'}
              sessionId={session.id}
              sessionVersion={session.version}
              parentRun={selectedSummary}
              frozen={frozen}
              onDirtyChange={onControlDirty}
              onRunCreated={selectRun}
            />
          </div>
          {runQuery.data ? (
            <ExperienceNote key={runQuery.data.id} run={runQuery.data} frozen={frozen} onDirtyChange={onNoteDirty} />
          ) : null}
        </aside>
      </div>
    </main>
  );
}
