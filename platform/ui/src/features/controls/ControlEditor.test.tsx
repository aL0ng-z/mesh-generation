import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import type { ControlChange, ControlPreview, ControlState, RunSummary } from '../../api/types';
import { ControlEditor } from './ControlEditor';

// 用可控的防抖替身精确控制「预检与当前草稿匹配」的窗口，
// 测试只依赖真实的查询键与缓存机制。
const debounce = vi.hoisted(() => ({ flush: (() => {}) as () => void }));
vi.mock('./useDebouncedValue', async () => {
  const { useEffect, useState } = await import('react');
  return {
    useDebouncedValue: <T,>(value: T): T => {
      const [debounced, setDebounced] = useState(value);
      useEffect(() => {
        debounce.flush = () => setDebounced(value);
        return () => {
          debounce.flush = () => {};
        };
      }, [value]);
      return debounced;
    },
  };
});

afterEach(() => {
  cleanup();
  debounce.flush = () => {};
  vi.restoreAllMocks();
});

const parentRun: RunSummary = {
  id: 'parent-1',
  session_id: 'session-1',
  sequence: 1,
  label: 'Baseline',
  status: 'SUCCEEDED',
  quality_status: 'UNKNOWN',
  preview_status: 'READY',
  created_at: '2026-08-09T00:00:00Z',
};

const controlState: ControlState = {
  parent_run_id: 'parent-1',
  controls: [
    {
      key: 'row/mesh_level',
      label: '网格层级',
      priority: 'P0',
      selector: 'row:#1',
      stage: 'wizard',
      availability: 'EDITABLE',
      value_type: 'enum',
      options: [
        { value: 'user', label: 'user' },
        { value: 'fine', label: 'fine' },
      ],
    },
    {
      key: 'row/target_points',
      label: '目标点数',
      priority: 'P0',
      selector: 'row:#1',
      stage: 'wizard',
      availability: 'LOCKED',
      reason: '需先显式设置 row/mesh_level=user',
      value_type: 'integer',
    },
    {
      key: 'row/expansion_ratio',
      label: '膨胀比',
      priority: 'P0',
      selector: 'row:#1',
      stage: 'wizard',
      availability: 'EDITABLE',
      value_type: 'number',
    },
  ],
  entities: ['row:#1'],
  stages: ['wizard'],
  topologies: [],
};

function previewFor(changes: readonly ControlChange[]): ControlPreview {
  const hasUser = changes.some(
    (change) => change.key === 'row/mesh_level' && change.op === 'set',
  );
  return {
    valid: hasUser,
    normalized_changes: [...changes],
    required_clears: [],
    warnings: [],
    errors: hasUser
      ? []
      : [{
        code: 'CONTROL_PREREQUISITE_NOT_MET',
        key: 'row/target_points',
        selector: 'row:#1',
        message: '控制 row/target_points 需要先显式设置 row/mesh_level=user',
      }],
    effective_availability: [
      { key: 'row/mesh_level', selector: 'row:#1', availability: 'EDITABLE', reason: null },
      {
        key: 'row/target_points',
        selector: 'row:#1',
        availability: hasUser ? 'EDITABLE' : 'LOCKED',
        reason: hasUser ? null : '需先显式设置 row/mesh_level=user',
      },
    ],
  };
}

function renderEditor() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ControlEditor
        sessionId="session-1"
        sessionVersion={1}
        parentRun={parentRun}
        frozen={false}
        onDirtyChange={() => {}}
        onRunCreated={() => {}}
      />
    </QueryClientProvider>,
  );
}

const targetInput = () => screen.getByRole('textbox', { name: 'row/target_points · row:#1' });
const ratioInput = () => screen.getByRole('textbox', { name: 'row/expansion_ratio · row:#1' });
const submitButton = () => screen.getByRole('button', { name: '创建不可变分支' });

function setupApi() {
  vi.spyOn(api, 'getControlState').mockResolvedValue(controlState);
  return vi.spyOn(api, 'previewControls').mockImplementation(
    async (_sessionId: string, _parentRunId: string, changes: ControlChange[]) => previewFor(changes),
  );
}

async function unlockTargetPoints() {
  await screen.findByRole('textbox', { name: 'row/target_points · row:#1' });
  const levelSelect = within(screen.getAllByRole('article')[0]).getByRole('combobox');
  fireEvent.change(levelSelect, { target: { value: 'user' } });
  act(() => debounce.flush());
  await waitFor(() => expect(targetInput()).not.toBeDisabled());
}

function lastPreviewChanges(previewSpy: ReturnType<typeof setupApi>): ControlChange[] {
  return (previewSpy.mock.calls.at(-1)?.[2] ?? []) as ControlChange[];
}

function hasSet(changes: readonly ControlChange[], key: string, value: unknown) {
  return changes.some((change) => change.key === key && change.op === 'set' && change.value === value);
}

it('同一草稿内设置前置项后解锁子项，撤销前置项后重新锁定', async () => {
  setupApi();
  renderEditor();

  expect(await screen.findByRole('textbox', { name: 'row/target_points · row:#1' })).toBeDisabled();

  const levelSelect = within(screen.getAllByRole('article')[0]).getByRole('combobox');
  fireEvent.change(levelSelect, { target: { value: 'user' } });
  // 预检尚未匹配当前草稿：子项仍按父快照锁定，不消费过期预检。
  expect(targetInput()).toBeDisabled();

  act(() => debounce.flush());
  await waitFor(() => expect(targetInput()).not.toBeDisabled());

  // 撤销前置项：过渡期立即回退锁定；空草稿匹配后仍不消费历史预检缓存。
  fireEvent.click(within(screen.getAllByRole('article')[0]).getByRole('button', { name: '撤销' }));
  expect(targetInput()).toBeDisabled();
  act(() => debounce.flush());
  expect(targetInput()).toBeDisabled();
});

it('预检过渡期正在编辑的行保持可编辑，匹配后按有效状态重新锁定', async () => {
  setupApi();
  renderEditor();

  await screen.findByRole('textbox', { name: 'row/target_points · row:#1' });
  fireEvent.change(within(screen.getAllByRole('article')[0]).getByRole('combobox'), { target: { value: 'user' } });
  act(() => debounce.flush());
  await waitFor(() => expect(targetInput()).not.toBeDisabled());

  // 解锁后修改子项值：草稿变化、预检未匹配时输入不被禁用。
  fireEvent.change(targetInput(), { target: { value: '500000' } });
  expect(targetInput()).not.toBeDisabled();

  act(() => debounce.flush());
  await waitFor(() => expect(targetInput()).not.toBeDisabled());

  // 撤销前置项后，匹配的预检把子项重新锁定（即使草稿中仍有该子项）。
  fireEvent.click(within(screen.getAllByRole('article')[0]).getByRole('button', { name: '撤销' }));
  act(() => debounce.flush());
  await waitFor(() => expect(targetInput()).toBeDisabled());
  expect(screen.getByText('需先显式设置 row/mesh_level=user')).toBeInTheDocument();
});

it('数值输入完整解析十进制：1e3 与 +10 进入草稿，输入框保留原始文本', async () => {
  const previewSpy = setupApi();
  renderEditor();
  await unlockTargetPoints();

  fireEvent.change(targetInput(), { target: { value: '1e3' } });
  expect(targetInput()).toHaveValue('1e3');
  act(() => debounce.flush());
  await waitFor(() => expect(hasSet(lastPreviewChanges(previewSpy), 'row/target_points', 1000)).toBe(true));

  fireEvent.change(targetInput(), { target: { value: '+10' } });
  expect(targetInput()).toHaveValue('+10');
  act(() => debounce.flush());
  await waitFor(() => expect(hasSet(lastPreviewChanges(previewSpy), 'row/target_points', 10)).toBe(true));
});

it('1.9 与超安全整数明确报错、不进入草稿并阻止提交', async () => {
  const previewSpy = setupApi();
  renderEditor();
  await unlockTargetPoints();

  fireEvent.change(targetInput(), { target: { value: '9007199254740991' } });
  act(() => debounce.flush());
  await waitFor(() => expect(hasSet(lastPreviewChanges(previewSpy), 'row/target_points', 9007199254740991)).toBe(true));
  await waitFor(() => expect(submitButton()).not.toBeDisabled());

  fireEvent.change(targetInput(), { target: { value: '9007199254740992' } });
  expect(targetInput()).toHaveValue('9007199254740992');
  expect(screen.getByText('超出安全整数范围（-9007199254740991 ~ 9007199254740991）')).toBeInTheDocument();
  expect(submitButton()).toBeDisabled();
  // 非法值不进入草稿：草稿未变，最后一次预检仍收到上一个合法值而非截断值。
  act(() => debounce.flush());
  await waitFor(() => expect(hasSet(lastPreviewChanges(previewSpy), 'row/target_points', 9007199254740991)).toBe(true));

  fireEvent.change(targetInput(), { target: { value: '1.9' } });
  expect(screen.getByText('必须是整数，不接受小数')).toBeInTheDocument();
  expect(submitButton()).toBeDisabled();
  expect(screen.getByText(/有 1 项数值输入未完成或无效，请修正后再提交/)).toBeInTheDocument();
});

it('空串、纯符号与未完成指数阻止提交：不清零、不自动清除草稿', async () => {
  const previewSpy = setupApi();
  renderEditor();
  await unlockTargetPoints();

  fireEvent.change(targetInput(), { target: { value: '500' } });
  act(() => debounce.flush());
  await waitFor(() => expect(hasSet(lastPreviewChanges(previewSpy), 'row/target_points', 500)).toBe(true));
  await waitFor(() => expect(submitButton()).not.toBeDisabled());

  fireEvent.change(targetInput(), { target: { value: '' } });
  expect(targetInput()).toHaveValue('');
  expect(targetInput()).not.toHaveValue('0');
  expect(submitButton()).toBeDisabled();
  expect(screen.getByText(/有 1 项数值输入未完成或无效，请修正后再提交/)).toBeInTheDocument();
  // 草稿中的 500 保留：撤销按钮仍在，未自动清除该控制。
  expect(within(screen.getAllByRole('article')[1]).getByRole('button', { name: '撤销' })).toBeInTheDocument();
  act(() => debounce.flush());
  await waitFor(() => expect(hasSet(lastPreviewChanges(previewSpy), 'row/target_points', 500)).toBe(true));

  fireEvent.change(targetInput(), { target: { value: '+' } });
  expect(targetInput()).toHaveValue('+');
  expect(submitButton()).toBeDisabled();

  fireEvent.change(targetInput(), { target: { value: '1e' } });
  expect(targetInput()).toHaveValue('1e');
  expect(submitButton()).toBeDisabled();

  // 修正为完整数值后恢复可提交
  fireEvent.change(targetInput(), { target: { value: '1e3' } });
  act(() => debounce.flush());
  await waitFor(() => expect(hasSet(lastPreviewChanges(previewSpy), 'row/target_points', 1000)).toBe(true));
  await waitFor(() => expect(submitButton()).not.toBeDisabled());
  expect(screen.queryByText(/项数值输入未完成或无效/)).not.toBeInTheDocument();
});

it('无既有草稿项的未完成输入不产生草稿项', async () => {
  const previewSpy = setupApi();
  renderEditor();
  await unlockTargetPoints();

  fireEvent.change(targetInput(), { target: { value: '1e' } });
  expect(targetInput()).toHaveValue('1e');
  expect(submitButton()).toBeDisabled();
  act(() => debounce.flush());
  await waitFor(() => {
    const changes = lastPreviewChanges(previewSpy);
    expect(changes.some((change) => change.key === 'row/target_points')).toBe(false);
  });
  // 仅解锁前置项在草稿中，未完成输入没有把控制项加进草稿
  expect(screen.getByText('1 项草稿')).toBeInTheDocument();

  // 修正为合法值后才进入草稿
  fireEvent.change(targetInput(), { target: { value: '3' } });
  act(() => debounce.flush());
  await waitFor(() => expect(hasSet(lastPreviewChanges(previewSpy), 'row/target_points', 3)).toBe(true));
});

it('粘贴与输入法组合输入过程不被解析改写', async () => {
  const previewSpy = setupApi();
  renderEditor();
  await unlockTargetPoints();

  // 模拟粘贴整段科学计数法文本：原样保留在输入框
  fireEvent.paste(targetInput());
  fireEvent.change(targetInput(), { target: { value: '1e3' } });
  expect(targetInput()).toHaveValue('1e3');

  // 输入法组合中间态（未完成指数）：不被改写成 0 或上一个解析值
  fireEvent.compositionStart(targetInput());
  fireEvent.change(targetInput(), { target: { value: '1e' } });
  expect(targetInput()).toHaveValue('1e');
  expect(submitButton()).toBeDisabled();

  // 组合结束提交完整文本
  fireEvent.compositionEnd(targetInput());
  fireEvent.change(targetInput(), { target: { value: '1e3' } });
  expect(targetInput()).toHaveValue('1e3');
  act(() => debounce.flush());
  await waitFor(() => expect(hasSet(lastPreviewChanges(previewSpy), 'row/target_points', 1000)).toBe(true));
  await waitFor(() => expect(submitButton()).not.toBeDisabled());
});

it('number 类型允许小数与科学计数法，不套用整数限制', async () => {
  const previewSpy = setupApi();
  renderEditor();
  await unlockTargetPoints();

  fireEvent.change(ratioInput(), { target: { value: '1.5e-3' } });
  expect(ratioInput()).toHaveValue('1.5e-3');
  act(() => debounce.flush());
  await waitFor(() => expect(hasSet(lastPreviewChanges(previewSpy), 'row/expansion_ratio', 0.0015)).toBe(true));

  fireEvent.change(ratioInput(), { target: { value: '1.9' } });
  act(() => debounce.flush());
  await waitFor(() => expect(hasSet(lastPreviewChanges(previewSpy), 'row/expansion_ratio', 1.9)).toBe(true));
  expect(screen.queryByText('必须是整数，不接受小数')).not.toBeInTheDocument();
});
