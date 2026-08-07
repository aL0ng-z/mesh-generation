import { FormEvent, useEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { api } from '../../api/client';
import styles from './UploadDialog.module.css';

interface Props {
  open: boolean;
  onClose: () => void;
}

export function UploadDialog({ open, onClose }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState('');
  const [expertSignature, setExpertSignature] = useState('');
  const [validation, setValidation] = useState('');

  const createMutation = useMutation({
    mutationFn: api.createSession,
    onSuccess: async (session) => {
      await queryClient.invalidateQueries({ queryKey: ['sessions'] });
      onClose();
      void navigate(`/sessions/${session.id}`);
    },
  });

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  function selectFile(next: File | null) {
    setFile(next);
    setValidation('');
    if (next && !title) setTitle(next.name.replace(/\.geomturbo$/i, ''));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!file) {
      setValidation('请选择 .geomTurbo 几何文件。');
      return;
    }
    if (!/\.geomturbo$/i.test(file.name)) {
      setValidation('文件扩展名必须是 .geomTurbo。');
      return;
    }
    if (!title.trim()) {
      setValidation('请输入会话标题。');
      return;
    }
    createMutation.mutate({ file, title: title.trim(), expertSignature: expertSignature.trim() });
  }

  return (
    <dialog
      ref={dialogRef}
      className={styles.dialog}
      aria-labelledby="upload-title"
      onCancel={(event) => {
        if (createMutation.isPending) event.preventDefault();
        else onClose();
      }}
      onClose={() => {
        if (open && !createMutation.isPending) onClose();
      }}
    >
      <form method="dialog" onSubmit={submit}>
        <header>
          <div>
            <p>新建共享会话</p>
            <h2 id="upload-title">上传几何并创建 baseline</h2>
          </div>
          <button type="button" aria-label="关闭" disabled={createMutation.isPending} onClick={onClose}>
            ×
          </button>
        </header>

        <label className={styles.dropzone}>
          <input
            type="file"
            accept=".geomTurbo"
            disabled={createMutation.isPending}
            onChange={(event) => selectFile(event.target.files?.[0] ?? null)}
          />
          <span className={styles.uploadIcon} aria-hidden="true">↑</span>
          <strong>{file ? file.name : '选择 .geomTurbo 文件'}</strong>
          <small>{file ? `${(file.size / 1024).toFixed(1)} KiB` : '文件会流式上传并在服务器解析'}</small>
        </label>

        <div className={styles.fields}>
          <label>
            <span>会话标题</span>
            <input
              value={title}
              maxLength={120}
              disabled={createMutation.isPending}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="例如 Rotor37 基准网格"
            />
          </label>
          <label>
            <span>专家署名（可选）</span>
            <input
              value={expertSignature}
              maxLength={80}
              disabled={createMutation.isPending}
              onChange={(event) => setExpertSignature(event.target.value)}
              placeholder="仅用于展示，不构成权限"
            />
          </label>
        </div>

        {validation ? <p className={styles.error} role="alert">{validation}</p> : null}
        {createMutation.isError ? (
          <p className={styles.error} role="alert">{(createMutation.error as Error).message}</p>
        ) : null}

        <footer>
          <button type="button" disabled={createMutation.isPending} onClick={onClose}>
            取消
          </button>
          <button className={styles.submit} type="submit" disabled={createMutation.isPending}>
            {createMutation.isPending ? '正在上传并解析…' : '创建会话'}
          </button>
        </footer>
      </form>
    </dialog>
  );
}
