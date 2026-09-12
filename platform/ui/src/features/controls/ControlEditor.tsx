import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, newRequestId } from '../../api/client';
import type {
  ControlAvailability,
  ControlChange,
  ControlItem,
  ControlPriority,
  RunSummary,
} from '../../api/types';
import { clearDraftValue, findDraftChange, removeDraftChange, setDraftValue } from './draft';
import { parseNumericText } from './numericInput';
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

interface EffectiveAvailability {
  availability: ControlAvailability;
  can_clear?: boolean;
  reason?: string | null;
}

// 数值输入保留的编辑态：用户原始文本 + 本地校验提示。
interface NumericInputState {
  text: string;
  issue?: string | null;
}

function ControlInput({
  item,
  change,
  disabled,
  onSet,
  numeric,
  onNumericText,
}: {
  item: ControlItem;
  change?: ControlChange;
  disabled: boolean;
  onSet: (value: unknown) => void;
  numeric?: NumericInputState;
  onNumericText: (raw: string) => void;
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
      <select value={String(visibleValue)} disabled={disabled} onChange={(event) => onSet(event.target.value)}>
        <option value="" disabled>请选择</option>
        {item.options.map((option) => <option key={String(option.value)} value={String(option.value)}>{option.label}</option>)}
      </select>
    );
  }
  // 数值输入用文本状态保存中间态：输入框始终显示用户原始文本（粘贴与输入法
  // 组合过程不被解析改写），每次变化时解析，可解析时才更新草稿。
  if (item.value_type === 'integer' || item.value_type === 'number') {
    const value = numeric ? numeric.text : String(visibleValue);
    return (
      <div className={styles.numericField}>
        <input
          type="text"
          inputMode="decimal"
          aria-label={`${item.key} · ${item.selector}`}
          value={value}
          disabled={disabled}
          placeholder={item.explicit ? '' : '由 AutoGrid 默认决定'}
          onChange={(event) => onNumericText(event.target.value)}
        />
        {numeric?.issue ? <small className={styles.inputIssue}>{numeric.issue}</small> : null}
      </div>
    );
  }
  return (
    <input
      type="text"
      value={String(visibleValue)}
      disabled={disabled}
      placeholder={item.explicit ? '' : '由 AutoGrid 默认决定'}
      onChange={(event) => onSet(event.target.value)}
    />
  );
}

function ControlRow({
  item,
  draft,
  frozen,
  effective,
  onChange,
  numeric,
  onNumericText,
  onResetNumeric,
}: {
  item: ControlItem;
  draft: readonly ControlChange[];
  frozen: boolean;
  effective?: EffectiveAvailability;
  onChange: (next: ControlChange[]) => void;
  numeric?: NumericInputState;
  onNumericText: (raw: string) => void;
  onResetNumeric: () => void;
}) {
  const change = findDraftChange(draft, item.key, item.selector);
  // 行可编辑性优先取与当前草稿匹配的预检结果（服务端是规则的唯一来源）；
  // 预检未返回或与草稿不匹配时不消费过期预检，仅回退到父快照目录状态。
  // 正在编辑的行在预检过渡期保持可编辑，避免输入中途被禁用。
  const availability = effective?.availability ?? item.availability;
  const reason = effective ? effective.reason : item.reason;
  const disabled = frozen || (effective
    ? effective.availability !== 'EDITABLE'
    : change?.op === 'set' ? false : item.availability !== 'EDITABLE');
  const canClear = !frozen && (effective?.can_clear ?? item.can_clear ?? (!disabled && item.explicit));
  return (
    <article className={styles.control} data-availability={availability}>
      <div className={styles.controlHead}>
        <div>
          <strong>{item.label}</strong>
          <code>{item.key} · {item.selector}</code>
        </div>
        <span>{availabilityText[availability]}</span>
      </div>
      {item.description ? <p>{item.description}</p> : null}
      {reason ? <p className={styles.reason}>{reason}</p> : null}
      <div className={styles.valueRow}>
        <ControlInput
          item={item}
          change={change}
          disabled={disabled}
          onSet={(value) => onChange(setDraftValue(draft, item.key, item.selector, value))}
          numeric={numeric}
          onNumericText={onNumericText}
        />
        {canClear ? (
          <button
            type="button"
            title="清除显式设置，恢复继承或默认值"
            onClick={() => {
              onChange(clearDraftValue(draft, item.key, item.selector));
              onResetNumeric();
            }}
          >
            清除
          </button>
        ) : null}
        {change || numeric ? (
          <button
            type="button"
            title="撤销这项草稿"
            onClick={() => {
              onChange(removeDraftChange(draft, item.key, item.selector));
              onResetNumeric();
            }}
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
  // 数值输入的中间态文本按 key -> selector 保存，与草稿分离：
  // 草稿只存解析后的数值，原始文本保留在输入框中直到撤销、清除或提交。
  const [numericInputs, setNumericInputs] = useState<Record<string, Record<string, NumericInputState>>>({});
  const debouncedDraft = useDebouncedValue(draft, 400);
  const dirty = draft.length > 0 || Object.keys(numericInputs).length > 0;

  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);

  const controlQuery = useQuery({
    queryKey: ['control-state', sessionId, parentRun?.id, parentRun?.status],
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
      setNumericInputs({});
      await queryClient.invalidateQueries({ queryKey: ['session', sessionId] });
      onRunCreated(run.id);
    },
  });

  // 仅消费与当前草稿匹配的预检结果：草稿为空或与防抖草稿不一致时，
  // 不应用任何预检数据，避免过期预检解锁不应编辑的行。
  const previewCurrent = JSON.stringify(debouncedDraft) === JSON.stringify(draft);
  const effectiveByKey = useMemo(() => {
    const map = new Map<string, Map<string, EffectiveAvailability>>();
    if (!previewCurrent || !debouncedDraft.length) return map;
    for (const entry of previewQuery.data?.effective_availability ?? []) {
      let inner = map.get(entry.key);
      if (!inner) {
        inner = new Map();
        map.set(entry.key, inner);
      }
      inner.set(entry.selector, entry);
    }
    return map;
  }, [previewCurrent, debouncedDraft, previewQuery.data]);

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

  const blockedCount = useMemo(
    () => Object.values(numericInputs).reduce(
      (count, bySelector) => count + Object.values(bySelector).filter((state) => state.issue).length,
      0,
    ),
    [numericInputs],
  );

  // 数值输入变化：文本原样保留；解析成功才更新草稿，
  // 未完成/非法输入仅显示提示并阻止提交，不修改也不自动清除草稿。
  function handleNumericText(item: ControlItem, raw: string) {
    const parsed = parseNumericText(raw, item.value_type === 'integer');
    if (parsed.status === 'ok') {
      setNumericInputs((prev) => ({
        ...prev,
        [item.key]: { ...(prev[item.key] ?? {}), [item.selector]: { text: raw } },
      }));
      setDraft(setDraftValue(draft, item.key, item.selector, parsed.value));
    } else {
      setNumericInputs((prev) => ({
        ...prev,
        [item.key]: { ...(prev[item.key] ?? {}), [item.selector]: { text: raw, issue: parsed.message } },
      }));
    }
  }

  function resetNumericInput(key: string, selector: string) {
    setNumericInputs((prev) => {
      const bySelector = prev[key];
      if (!bySelector || !(selector in bySelector)) return prev;
      const nextBySelector = { ...bySelector };
      delete nextBySelector[selector];
      const next = { ...prev };
      if (Object.keys(nextBySelector).length) {
        next[key] = nextBySelector;
      } else {
        delete next[key];
      }
      return next;
    });
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
          {group('P0').map((item) => <ControlRow key={`${item.key}:${item.selector}`} item={item} draft={draft} frozen={!canBranch} effective={effectiveByKey.get(item.key)?.get(item.selector)} onChange={setDraft} numeric={numericInputs[item.key]?.[item.selector]} onNumericText={(raw) => handleNumericText(item, raw)} onResetNumeric={() => resetNumericInput(item.key, item.selector)} />)}
        </section>
        {(['P1', 'P2'] as const).map((priority) => (
          <details key={priority} open={Boolean(search)}>
            <summary>{priority} · {priority === 'P1' ? '进阶控制' : '专家控制'} <span>{group(priority).length}</span></summary>
            {group(priority).map((item) => <ControlRow key={`${item.key}:${item.selector}`} item={item} draft={draft} frozen={!canBranch} effective={effectiveByKey.get(item.key)?.get(item.selector)} onChange={setDraft} numeric={numericInputs[item.key]?.[item.selector]} onNumericText={(raw) => handleNumericText(item, raw)} onResetNumeric={() => resetNumericInput(item.key, item.selector)} />)}
          </details>
        ))}
        {!controlQuery.isPending && !filtered.length ? <p className={styles.loading}>没有符合筛选条件的控制项。</p> : null}
      </div>

      <footer>
        <div className={styles.preview} aria-live="polite">
          {blockedCount > 0 ? `有 ${blockedCount} 项数值输入未完成或无效，请修正后再提交。` : null}
          {!draft.length ? '修改控制后，服务端会自动预检依赖与拓扑。' : null}
          {draft.length && (!previewCurrent || previewQuery.isFetching) ? '正在进行服务端预检…' : null}
          {draft.length && previewQuery.data && previewCurrent ? (
            previewQuery.data.valid
              ? `预检通过${previewQuery.data.required_clears?.length ? `，需确认清除 ${previewQuery.data.required_clears.length} 项覆盖` : ''}`
              : `预检未通过：${previewQuery.data.errors?.map((issue) => issue.message).join('；') || '请检查草稿'}`
          ) : null}
          {previewQuery.isError ? `预检失败：${(previewQuery.error as Error).message}` : null}
          {createMutation.isError ? `创建分支失败：${(createMutation.error as Error).message}` : null}
        </div>
        <div className={styles.actions}>
          <button type="button" disabled={!dirty || createMutation.isPending} onClick={() => { setDraft([]); setNumericInputs({}); }}>放弃草稿</button>
          <button
            className={styles.submit}
            type="button"
            disabled={!canBranch || !draft.length || !previewCurrent || !previewQuery.data?.valid || previewQuery.isFetching || createMutation.isPending || blockedCount > 0}
            onClick={submit}
          >
            {createMutation.isPending ? '正在创建…' : '创建不可变分支'}
          </button>
        </div>
      </footer>
    </section>
  );
}
