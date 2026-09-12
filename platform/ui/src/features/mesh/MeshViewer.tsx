import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { MeshBlock, RunDetail } from '../../api/types';
import { MeshCanvas, type MeshAsset } from './MeshCanvas';
import { manifestPollingInterval, manifestQueryKey } from './manifestPolling';
import styles from './MeshViewer.module.css';

interface Props {
  run?: RunDetail;
  cameraGroup?: string;
  compact?: boolean;
}

function sliceRange(block: MeshBlock | undefined, axis: 'I' | 'J' | 'K') {
  const publicRange = block?.slices?.[axis];
  if (publicRange) return publicRange;
  const rawRange = block?.index_ranges?.[axis];
  if (rawRange) return { minimum: rawRange[0], maximum: rawRange[1] };
  const dimension = Array.isArray(block?.dimensions)
    ? block.dimensions[{ I: 0, J: 1, K: 2 }[axis]]
    : block?.dimensions[axis] ?? 1;
  return { minimum: 0, maximum: Math.max(0, dimension - 1) };
}

function supportsMode(block: MeshBlock, mode: 'surface' | 'wireframe') {
  const publicFlag = mode === 'surface' ? block.surface : block.wireframe;
  return publicFlag ?? block.modes?.includes(mode) ?? false;
}

function dimensionsLabel(block: MeshBlock) {
  return Array.isArray(block.dimensions)
    ? block.dimensions.join('×')
    : `${block.dimensions.I}×${block.dimensions.J}×${block.dimensions.K}`;
}

export function MeshViewer({ run, cameraGroup, compact = false }: Props) {
  const manifestQuery = useQuery({
    queryKey: manifestQueryKey(run),
    queryFn: () => api.getMeshManifest(run!.id),
    enabled: Boolean(run?.id && run.status === 'SUCCEEDED'),
    staleTime: 30_000,
    refetchInterval: (query) => manifestPollingInterval(run, query.state.data),
  });
  const manifest = manifestQuery.data;
  const [mode, setMode] = useState<'surface' | 'wireframe'>('surface');
  const [hidden, setHidden] = useState<Set<string>>(() => new Set());
  const [sliceEnabled, setSliceEnabled] = useState(false);
  const [sliceBlock, setSliceBlock] = useState('');
  const [sliceAxis, setSliceAxis] = useState<'I' | 'J' | 'K'>('I');
  const block = manifest?.blocks.find((item) => item.id === sliceBlock) ?? manifest?.blocks[0];
  const range = sliceRange(block, sliceAxis);
  const [sliceIndex, setSliceIndex] = useState(0);

  const assets = useMemo<MeshAsset[]>(() => {
    if (!run || !manifest) return [];
    const result = manifest.blocks
      .filter((item) => !hidden.has(item.id) && supportsMode(item, mode))
      .map((item) => ({
        key: `${item.id}:${mode}`,
        url: api.meshBlockUrl(run.id, item.id, mode),
        wireframe: mode === 'wireframe',
      }));
    if (sliceEnabled && block) {
      const safeIndex = Math.min(range.maximum, Math.max(range.minimum, sliceIndex));
      result.push({
        key: `${block.id}:${sliceAxis}:${safeIndex}`,
        url: api.meshSliceUrl(run.id, block.id, sliceAxis, safeIndex),
        wireframe: true,
      });
    }
    return result;
  }, [block, hidden, manifest, mode, range.maximum, range.minimum, run, sliceAxis, sliceEnabled, sliceIndex]);

  if (!run) return <div className={styles.unavailable}>请先选择运行节点。</div>;
  if (run.status !== 'SUCCEEDED') {
    return <div className={styles.unavailable}>运行成功后才会生成网格预览。当前状态：{run.status}</div>;
  }
  if (manifestQuery.isPending) return <div className={styles.unavailable}>正在读取预览 manifest…</div>;
  if (manifestQuery.isError) return <div className={styles.unavailable}>Viewer manifest 加载失败：{(manifestQuery.error as Error).message}</div>;
  if (manifest?.status === 'PENDING' || (run.preview_status === 'PENDING' && manifest?.status !== 'READY' && manifest?.status !== 'FAILED')) {
    return <div className={styles.unavailable}>网格运行已成功，正在生成三维预览资产；页面每 3 秒自动刷新…</div>;
  }
  if (!manifest || manifest.available === false || manifest.status !== 'READY' || !manifest.blocks.length) {
    return (
      <div className={styles.unavailable}>
        <strong>三维预览不可用</strong>
        <span>{manifest?.reason || 'CGNS 预览尚未生成或格式不受支持。'}</span>
        <small>这不会改变网格运行结果；质量报告和原始产物仍可使用。</small>
      </div>
    );
  }

  return (
    <section className={styles.viewer} data-compact={compact} aria-label="真实网格三维查看器">
      <div className={styles.tools}>
        <div className={styles.segmented} aria-label="预览模式">
          <button type="button" aria-pressed={mode === 'surface'} onClick={() => setMode('surface')}>表面</button>
          <button type="button" aria-pressed={mode === 'wireframe'} onClick={() => setMode('wireframe')}>线框</button>
        </div>
        {!compact ? (
          <label className={styles.sliceToggle}>
            <input type="checkbox" checked={sliceEnabled} onChange={(event) => setSliceEnabled(event.target.checked)} />
            I/J/K 切片
          </label>
        ) : null}
        <span>{manifest.blocks.length} blocks</span>
      </div>

      {!compact ? (
        <div className={styles.manifestControls}>
          <div className={styles.blocks}>
            {manifest.blocks.map((item) => (
              <label key={item.id}>
                <input
                  type="checkbox"
                  checked={!hidden.has(item.id)}
                  onChange={(event) => {
                    setHidden((current) => {
                      const next = new Set(current);
                      if (event.target.checked) next.delete(item.id);
                      else next.add(item.id);
                      return next;
                    });
                  }}
                />
                {item.name} <small>{dimensionsLabel(item)}</small>
              </label>
            ))}
          </div>
          {sliceEnabled && manifest.capabilities?.slice !== false ? (
            <div className={styles.sliceControls}>
              <select aria-label="切片网格块" value={block?.id ?? ''} onChange={(event) => { setSliceBlock(event.target.value); setSliceIndex(0); }}>
                {manifest.blocks.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
              </select>
              <select aria-label="切片轴向" value={sliceAxis} onChange={(event) => { setSliceAxis(event.target.value as 'I' | 'J' | 'K'); setSliceIndex(0); }}>
                <option>I</option><option>J</option><option>K</option>
              </select>
              <input
                aria-label={`${sliceAxis} 切片索引`}
                type="range"
                min={range.minimum}
                max={range.maximum}
                value={Math.min(range.maximum, Math.max(range.minimum, sliceIndex))}
                onChange={(event) => setSliceIndex(Number(event.target.value))}
              />
              <output>{Math.min(range.maximum, Math.max(range.minimum, sliceIndex))}（{manifest.capabilities?.slice_index_base ?? 0} 基）</output>
            </div>
          ) : null}
        </div>
      ) : null}

      {assets.length ? <MeshCanvas assets={assets} cameraGroup={cameraGroup} /> : <div className={styles.unavailable}>所有 block 均已隐藏。</div>}
    </section>
  );
}
