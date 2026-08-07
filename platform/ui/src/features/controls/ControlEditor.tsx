import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, newRequestId } from '../../api/client';
import type {
  ControlChange,
  ControlItem,
  ControlPriority,
  RunSummary,
} from '../../api/types';
import { clearDraftValue, findDraftChange, removeDraftChange, setDraftValue } from './draft';
import { useDebouncedValue } from './useDebouncedValue';
import styles from './ControlEditor.module.css';

interface Props {
  sessionId: string;
  sessionVersion: number;
  parentRun?: RunSummary;
  frozen: boolean;
  onDirtyChange: (dirty: boolean) => void;
  onRunCreated: (runId: string) => void;
}

const availabilityText = {
  EDITABLE: '可编辑',
  LOCKED: '已锁定',
  NOT_APPLICABLE: '不适用',
} as const;

function parseInputValue(item: ControlItem, raw: string | boolean): unknown {
  if (item.value_type === 'boolean') return Boolean(raw);
  if (item.value_type === 'integer') return Number.parseInt(String(raw), 10);
  if (item.value_type === 'number') return Number(raw);
  return raw;
}

function ControlInput({
  item,
  change,
  disabled,
  onSet,
}: {
  item: ControlItem;
  change?: ControlChange;
  disabled: boolean;
  onSet: (value: unknown) => void;
}) {
  const visibleValue = change?.op === 'set' ? change.value : item.value ?? (item.explicit ? item.inherited_value : '') ?? '';
  if (item.value_type === 'boolean') {
    const checked = visibleValue === true || visibleValue === 'true' || visibleValue === 1;
    return (
      <label className={styles.switch}>
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onSet(event.target.checked)}
        />
        <span>{checked ? '开启' : '关闭'}</span>
      </label>
    );
  }
  if (item.value_type === 'enum' && item.options?.length) {
    return (
      <select value={String(visibleValue)} disabled={disabled} onChange={(event) => onSet(parseInputValue(item, event.target.value))}>
        <option value="" disabled>请选择</option>
        {item.options.map((option) => <option key={String(option.value)} value={String(option.value)}>{option.label}</option>)}
      </select>
    );
  }
  return (
    <input
      type={item.value_type === 'integer' || item.value_type === 'number' ? 'number' : 'text'}
      value={String(visibleValue)}
      min={item.minimum ?? undefined}
      max={item.maximum ?? undefined}
      step={item.step ?? (item.value_type === 'integer' ? 1 : undefined)}
      disabled={disabled}
      placeholder={item.explicit ? '' : '由 AutoGrid 默认决定'}
      onChange={(event) => onSet(parseInputValue(item, event.target.value))}
    />
  );
}

function ControlRow({
  item,
  draft,
  frozen,
  onChange,
}: {
  item: ControlItem;
  draft: readonly ControlChange[];
  frozen: boolean;
  onChange: (next: ControlChange[]) => void;
}) {
  const change = findDraftChange(draft, item.key, item.selector);
  const disabled = frozen || item.availability !== 'EDITABLE';
  return (
    <article className={styles.control} data-availability={item.availability}>
      <div className={styles.controlHead}>
        <div>
          <strong>{item.label}</strong>
          <code>{item.key} · {item.selector}</code>
        </div>
        <span>{availabilityText[item.availability]}</span>
      </div>
      {item.description ? <p>{item.description}</p> : null}
      {item.reason ? <p className={styles.reason}>{item.reason}</p> : null}
      <div className={styles.valueRow}>
        <ControlInput
          item={item}
          change={change}
          disabled={disabled}
          onSet={(value) => onChange(setDraftValue(draft, item.key, item.selector, value))}
        />
        {!disabled && item.explicit ? (
          <button
            type="button"
            title="清除显式设置，恢复继承或默认值"
            onClick={() => onChange(clearDraftValue(draft, item.key, item.selector))}
          >
            清除
          </button>
        ) : null}
        {change ? (
          <button
            type="button"
            title="撤销这项草稿"
            onClick={() => onChange(removeDraftChange(draft, item.key, item.selector))}
          >
            撤销
          </button>
        ) : null}
      </div>
      {!item.explicit && !change ? <small className={styles.defaultText}>由 AutoGrid 默认决定</small> : null}
    </article>
  );
}

export function ControlEditor({
  sessionId,
  sessionVersion,
  parentRun,
  frozen,
  onDirtyChange,
  onRunCreated,
}: Props) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<ControlChange[]>([]);
  const [search, setSearch] = useState('');
  const [entity, setEntity] = useState('ALL');
  const [target, setTarget] = useState('ALL');
  const [stage, setStage] = useState('ALL');
  const [topology, setTopology] = useState('ALL');
  const debouncedDraft = useDebouncedValue(draft, 400);

  useEffect(() => onDirtyChange(draft.length > 0), [draft.length, onDirtyChange]);

  const controlQuery = useQuery({
    queryKey: ['control-state', sessionId, parentRun?.id],
    queryFn: () => api.getControlState(sessionId, parentRun!.id),
    enabled: Boolean(parentRun?.id),
  });

  const previewQuery = useQuery({
    queryKey: ['control-preview', sessionId, parentRun?.id, debouncedDraft],
    queryFn: () => api.previewControls(sessionId, parentRun!.id, debouncedDraft),
    enabled: Boolean(parentRun?.id && debouncedDraft.length),
    staleTime: Number.POSITIVE_INFINITY,
  });

  const createMutation = useMutation({
    mutationFn: ({ changes, confirmRequiredClears }: { changes: ControlChange[]; confirmRequiredClears: boolean }) => api.createRun({
      sessionId,
      parentRunId: parentRun!.id,
      changes,
      expectedVersion: sessionVersion,
      requestId: newRequestId(),
      confirmRequiredClears,
    }),
    onSuccess: async (run) => {
      setDraft([]);
      await queryClient.invalidateQueries({ queryKey: ['session', sessionId] });
      onRunCreated(run.id);
    },
  });

  const controls = useMemo(() => controlQuery.data?.controls ?? [], [controlQuery.data?.controls]);
  const filtered = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase('zh-CN');
    return controls.filter((item) => {
      const text = `${item.label} ${item.key} ${item.description ?? ''} ${item.selector}`.toLocaleLowerCase('zh-CN');
      return (!needle || text.includes(needle))
        && (entity === 'ALL' || item.entity === entity)
        && (target === 'ALL' || item.selector === target)
        && (stage === 'ALL' || item.stage === stage)
        && (topology === 'ALL' || item.topology?.split(',').includes(topology));
    });
  }, [controls, entity, search, stage, target, topology]);

  const entityKinds = useMemo(
    () => [...new Set(controls.map((item) => item.entity).filter((value): value is string => Boolean(value)))].sort(),
    [controls],
  );

  function group(priority: ControlPriority) {
    return filtered.filter((item) => item.priority === priority);
  }

  function submit() {
    const preview = previewQuery.data;
    if (!preview?.valid || previewQuery.isFetching || !draft.length) return;
    const clears = preview.required_clears ?? [];
    if (clears.length) {
      const accepted = window.confirm(
        `本次父控制变化会清除 ${clears.length} 项子控制覆盖。\n\n继续将把这些 clear 操作与草稿一并提交，原运行节点不会被修改。`,
      );
      if (!accepted) return;
    }
    createMutation.mutate({
      changes: preview.normalized_changes ?? draft,
      confirmRequiredClears: clears.length > 0,
    });
  }

  if (!parentRun) return <div className={styles.placeholder}>请先从左侧选择运行节点。</div>;

  const canBranch = parentRun.status === 'SUCCEEDED' && !frozen;
  const previewCurrent = JSON.stringify(debouncedDraft) === JSON.stringify(draft);

  return (
    <section className={styles.editor} aria-labelledby="controls-title">
      <header>
        <div>
          <p>CONTROL DRAFT</p>
          <h2 id="controls-title">控制编辑器</h2>
        </div>
        {draft.length ? <span>{draft.length} 项草稿</span> : null}
      </header>

      {frozen ? <div className={styles.frozen}>会话已冻结，控制只读。</div> : null}
      {!frozen && parentRun.status !== 'SUCCEEDED' ? (
        <div className={styles.frozen}>仅成功运行可作为新分支的父节点。</div>
      ) : null}

      <div className={styles.filters}>
        <input aria-label="搜索全部控制" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索键、名称或说明…" />
        <div>
          <select aria-label="几何实体" value={entity} onChange={(event) => setEntity(event.target.value)}>
            <option value="ALL">全部实体</option>
            {entityKinds.map((value) => <option key={value}>{value}</option>)}
          </select>
          <select aria-label="精确目标" value={target} onChange={(event) => setTarget(event.target.value)}>
            <option value="ALL">全部目标</option>
            {(controlQuery.data?.entities ?? []).map((value) => <option key={value}>{value}</option>)}
          </select>
          <select aria-label="阶段" value={stage} onChange={(event) => setStage(event.target.value)}>
            <option value="ALL">全部阶段</option>
            {(controlQuery.data?.stages ?? []).map((value) => <option key={value}>{value}</option>)}
          </select>
          <select aria-label="拓扑" value={topology} onChange={(event) => setTopology(event.target.value)}>
            <option value="ALL">全部拓扑</option>
            {(controlQuery.data?.topologies ?? []).map((value) => <option key={value}>{value}</option>)}
          </select>
        </div>
      </div>

      {controlQuery.isPending ? <p className={styles.loading}>正在读取控制目录…</p> : null}
      {controlQuery.isError ? <p className={styles.error}>{(controlQuery.error as Error).message}</p> : null}

      <div className={styles.controlList}>
        <section>
          <h3>P0 · 常用控制 <span>{group('P0').length}</span></h3>
          {group('P0').map((item) => <ControlRow key={`${item.key}:${item.selector}`} item={item} draft={draft} frozen={!canBranch} onChange={setDraft} />)}
        </section>
        {(['P1', 'P2'] as const).map((priority) => (
          <details key={priority} open={Boolean(search)}>
            <summary>{priority} · {priority === 'P1' ? '进阶控制' : '专家控制'} <span>{group(priority).length}</span></summary>
            {group(priority).map((item) => <ControlRow key={`${item.key}:${item.selector}`} item={item} draft={draft} frozen={!canBranch} onChange={setDraft} />)}
          </details>
        ))}
        {!controlQuery.isPending && !filtered.length ? <p className={styles.loading}>没有符合筛选条件的控制项。</p> : null}
      </div>

      <footer>
        <div className={styles.preview} aria-live="polite">
          {!draft.length ? '修改控制后，服务端会自动预检依赖与拓扑。' : null}
          {draft.length && (!previewCurrent || previewQuery.isFetching) ? '正在进行服务端预检…' : null}
          {previewQuery.data && previewCurrent ? (
            previewQuery.data.valid
              ? `预检通过${previewQuery.data.required_clears?.length ? `，需确认清除 ${previewQuery.data.required_clears.length} 项覆盖` : ''}`
              : `预检未通过：${previewQuery.data.errors?.map((issue) => issue.message).join('；') || '请检查草稿'}`
          ) : null}
          {previewQuery.isError ? `预检失败：${(previewQuery.error as Error).message}` : null}
          {createMutation.isError ? `创建分支失败：${(createMutation.error as Error).message}` : null}
        </div>
        <div className={styles.actions}>
          <button type="button" disabled={!draft.length || createMutation.isPending} onClick={() => setDraft([])}>放弃草稿</button>
          <button
            className={styles.submit}
            type="button"
            disabled={!canBranch || !draft.length || !previewCurrent || !previewQuery.data?.valid || previewQuery.isFetching || createMutation.isPending}
            onClick={submit}
          >
            {createMutation.isPending ? '正在创建…' : '创建不可变分支'}
          </button>
        </div>
      </footer>
    </section>
  );
}
