import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ApiError, api } from '../../api/client';
import type { RunDetail } from '../../api/types';
import { ExperienceNote } from './ExperienceNote';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const run: RunDetail = {
  id: 'run-1', session_id: 'session-1', sequence: 1, status: 'SUCCEEDED',
  quality_status: 'PASS', created_at: '', experience_note: '原始经验', note_version: 1,
};

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const onDirtyChange = vi.fn();
  const node = (value: RunDetail) => <QueryClientProvider client={client}><ExperienceNote run={value} frozen={false} onDirtyChange={onDirtyChange} /></QueryClientProvider>;
  const view = render(node(run));
  return { client, onDirtyChange, rerender: (value: RunDetail) => view.rerender(node(value)) };
}

it('编辑中的 v1 草稿遇到远端 v2 仍提交 v1，冲突保留草稿，撤销恢复远端', async () => {
  const save = vi.spyOn(api, 'updateExperienceNote').mockRejectedValue(new ApiError(409, { error: { message: '版本冲突' } }));
  const view = setup();
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '本地草稿' } });
  view.rerender({ ...run, experience_note: '其他专家的新经验', note_version: 2 });
  fireEvent.click(screen.getByRole('button', { name: '保存经验' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('远端经验文本已更新');
  expect(save).toHaveBeenCalledWith(run.id, '本地草稿', 1);
  expect(save).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('textbox')).toHaveValue('本地草稿');
  fireEvent.click(screen.getByRole('button', { name: '撤销' }));
  expect(screen.getByRole('textbox')).toHaveValue('其他专家的新经验');
  expect(view.onDirtyChange).toHaveBeenLastCalledWith(false);
});

it('没有本地修改时同步远端文本和版本', async () => {
  const save = vi.spyOn(api, 'updateExperienceNote').mockResolvedValue({ ...run, note_version: 3, experience_note: '基于新版编辑' });
  const view = setup();
  view.rerender({ ...run, experience_note: '新版经验', note_version: 2 });
  expect(screen.getByRole('textbox')).toHaveValue('新版经验');
  expect(view.onDirtyChange).toHaveBeenLastCalledWith(false);
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '基于新版编辑' } });
  fireEvent.click(screen.getByRole('button', { name: '保存经验' }));
  await waitFor(() => expect(save).toHaveBeenCalledWith(run.id, '基于新版编辑', 2));
});

it('保存成功立即采用响应文本与版本作为下一次编辑基准', async () => {
  const updated = { ...run, experience_note: '已保存文本', note_version: 2 };
  const save = vi.spyOn(api, 'updateExperienceNote').mockResolvedValue(updated);
  const view = setup();
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '已保存文本' } });
  fireEvent.click(screen.getByRole('button', { name: '保存经验' }));
  await waitFor(() => expect(view.client.getQueryData(['run', run.id])).toEqual(updated));
  expect(screen.getByRole('textbox')).toHaveValue('已保存文本');
  expect(screen.getByRole('button', { name: '保存经验' })).toBeDisabled();
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '继续编辑' } });
  fireEvent.click(screen.getByRole('button', { name: '保存经验' }));
  await waitFor(() => expect(save).toHaveBeenLastCalledWith(run.id, '继续编辑', 2));
});
