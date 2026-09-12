import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ApiError, api } from '../../api/client';
import type { RunDetail } from '../../api/types';
import styles from './ExperienceNote.module.css';

interface Props {
  run: RunDetail;
  frozen: boolean;
  onDirtyChange: (dirty: boolean) => void;
}

export function ExperienceNote({ run, frozen, onDirtyChange }: Props) {
  const saved = run.experience_note ?? '';
  const version = run.note_version ?? 0;
  const [editing, setEditing] = useState({ baseText: saved, baseVersion: version, text: saved });
  const queryClient = useQueryClient();
  const draft = editing.text;
  const dirty = draft !== editing.baseText;

  // 仅没有本地修改时跟随远端；编辑中的草稿必须保留开始编辑时的版本。
  if (!dirty && (version > editing.baseVersion || (version === editing.baseVersion && editing.baseText !== saved))) {
    setEditing({ baseText: saved, baseVersion: version, text: saved });
  }

  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);

  const saveMutation = useMutation({
    mutationFn: () => api.updateExperienceNote(run.id, draft, editing.baseVersion),
    onSuccess: async (updated) => {
      const text = updated.experience_note ?? '';
      setEditing({ baseText: text, baseVersion: updated.note_version ?? 0, text });
      queryClient.setQueryData(['run', run.id], updated);
      await queryClient.invalidateQueries({ queryKey: ['run', run.id] });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        void queryClient.invalidateQueries({ queryKey: ['run', run.id] });
      }
    },
  });

  const editable = run.status === 'SUCCEEDED' && !frozen;
  return (
    <section className={styles.note} aria-labelledby="experience-title">
      <header>
        <div>
          <p>EXPERT NOTE</p>
          <h2 id="experience-title">经验文本</h2>
        </div>
        <span>v{run.note_version ?? 0}</span>
      </header>
      <textarea
        value={draft}
        disabled={!editable || saveMutation.isPending}
        maxLength={20_000}
        placeholder={editable ? '记录这一轮网格的适用工况、判断依据与后续建议…' : '当前运行没有可编辑的经验文本。'}
        onChange={(event) => setEditing({ ...editing, text: event.target.value })}
      />
      {saveMutation.isError ? <p role="alert">{saveMutation.error instanceof ApiError && saveMutation.error.status === 409 ? '远端经验文本已更新，本地草稿已保留；请核对后撤销并重新编辑。' : saveMutation.error.message}</p> : null}
      <footer>
        <small>{draft.length.toLocaleString('zh-CN')} / 20,000</small>
        <div>
          <button type="button" disabled={!editable || !draft || saveMutation.isPending} onClick={() => setEditing({ ...editing, text: '' })}>清空</button>
          <button type="button" disabled={!editable || !dirty || saveMutation.isPending} onClick={() => { setEditing({ baseText: saved, baseVersion: version, text: saved }); saveMutation.reset(); }}>撤销</button>
          <button className={styles.save} type="button" disabled={!editable || !dirty || saveMutation.isPending} onClick={() => saveMutation.mutate()}>
            {saveMutation.isPending ? '保存中…' : '保存经验'}
          </button>
        </div>
      </footer>
    </section>
  );
}
