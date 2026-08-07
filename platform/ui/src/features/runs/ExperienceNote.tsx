import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { RunDetail } from '../../api/types';
import styles from './ExperienceNote.module.css';

interface Props {
  run: RunDetail;
  frozen: boolean;
  onDirtyChange: (dirty: boolean) => void;
}

export function ExperienceNote({ run, frozen, onDirtyChange }: Props) {
  const saved = run.experience_note ?? '';
  const [draft, setDraft] = useState(saved);
  const queryClient = useQueryClient();
  const dirty = draft !== saved;

  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);

  const saveMutation = useMutation({
    mutationFn: () => api.updateExperienceNote(run.id, draft, run.note_version ?? 0),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['run', run.id] });
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
        onChange={(event) => setDraft(event.target.value)}
      />
      {saveMutation.isError ? <p role="alert">{(saveMutation.error as Error).message}</p> : null}
      <footer>
        <small>{draft.length.toLocaleString('zh-CN')} / 20,000</small>
        <div>
          <button type="button" disabled={!editable || !draft || saveMutation.isPending} onClick={() => setDraft('')}>清空</button>
          <button type="button" disabled={!editable || !dirty || saveMutation.isPending} onClick={() => setDraft(saved)}>撤销</button>
          <button className={styles.save} type="button" disabled={!editable || !dirty || saveMutation.isPending} onClick={() => saveMutation.mutate()}>
            {saveMutation.isPending ? '保存中…' : '保存经验'}
          </button>
        </div>
      </footer>
    </section>
  );
}
