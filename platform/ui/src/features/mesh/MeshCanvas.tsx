import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { loadVtkRuntime } from './vtkRuntime';
import styles from './MeshViewer.module.css';

export interface MeshAsset {
  key: string;
  url: string;
  wireframe?: boolean;
}

interface CameraState {
  source: string;
  position: number[];
  focalPoint: number[];
  viewUp: number[];
  parallelScale: number;
}

interface Props {
  assets: MeshAsset[];
  cameraGroup?: string;
}

const colors = [
  [0.38, 0.78, 0.74],
  [0.54, 0.72, 0.88],
  [0.87, 0.67, 0.37],
  [0.72, 0.59, 0.83],
];

export function MeshCanvas({ assets, cameraGroup }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const sourceId = useId();
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [message, setMessage] = useState('正在加载三维运行时与预览资产…');
  const signature = useMemo(() => assets.map((asset) => `${asset.key}:${asset.url}`).join('|'), [assets]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !assets.length) return;
    let cancelled = false;
    // 每创建一项资源立即登记释放动作；dispose 幂等，按登记逆序释放（后创建先释放）。
    const releases: Array<() => void> = [];
    let disposed = false;
    const dispose = () => {
      if (disposed) return;
      disposed = true;
      [...releases].reverse().forEach((release) => release());
    };
    const register = <T,>(value: T, release: (value: T) => void): T => {
      if (disposed) release(value);
      else releases.push(() => release(value));
      return value;
    };
    // 资产请求统一由本 effect 的 AbortController 管理，卸载/失败即中止在途请求。
    const controller = register(new AbortController(), (item) => item.abort());
    setState('loading');
    setMessage('正在加载三维运行时与预览资产…');

    void (async () => {
      try {
        const vtk = await loadVtkRuntime();
        if (cancelled) return;
        const view = register(
          vtk.GenericRenderWindow.newInstance({ background: [0.035, 0.065, 0.09] }),
          (item) => item.delete(),
        );
        view.setContainer(container);
        view.resize();
        const renderer = register(view.getRenderer(), (item) => item.delete());
        const renderWindow = register(view.getRenderWindow(), (item) => item.delete());

        const responses = await Promise.all(assets.map(async (asset) => {
          const response = await fetch(asset.url, {
            headers: { Accept: 'application/vnd.vtk.vtp+xml, application/xml' },
            signal: controller.signal,
          });
          if (!response.ok) throw new Error(`预览资产请求失败（HTTP ${response.status}）`);
          return response.arrayBuffer();
        })).catch((error) => {
          controller.abort(); // 单个请求失败时取消同批剩余请求
          throw error;
        });
        if (cancelled) return;

        responses.forEach((buffer, index) => {
          const reader = register(vtk.XMLPolyDataReader.newInstance(), (item) => item.delete());
          reader.parseAsArrayBuffer(buffer);
          const mapper = register(vtk.Mapper.newInstance(), (item) => item.delete());
          mapper.setInputConnection(reader.getOutputPort());
          const actor = register(vtk.Actor.newInstance(), (item) => item.delete());
          actor.setMapper(mapper);
          const color = colors[index % colors.length] ?? colors[0]!;
          actor.getProperty().setColor(...color);
          actor.getProperty().setOpacity(assets[index]?.wireframe ? 0.92 : 0.88);
          if (assets[index]?.wireframe) actor.getProperty().setLineWidth(1.25);
          renderer.addActor(actor);
        });

        renderer.resetCamera();
        renderWindow.render();
        const camera = renderer.getActiveCamera();
        let applyingExternalCamera = false;
        const eventName = cameraGroup ? `mesh-camera:${cameraGroup}` : '';
        register(camera.onModified(() => {
          if (!eventName || applyingExternalCamera) return;
          const detail: CameraState = {
            source: sourceId,
            position: [...camera.getPosition()],
            focalPoint: [...camera.getFocalPoint()],
            viewUp: [...camera.getViewUp()],
            parallelScale: camera.getParallelScale(),
          };
          window.dispatchEvent(new CustomEvent(eventName, { detail }));
        }), (subscription) => subscription.unsubscribe());
        const receive = (event: Event) => {
          const detail = (event as CustomEvent<CameraState>).detail;
          if (!detail || detail.source === sourceId) return;
          applyingExternalCamera = true;
          camera.setPosition(...detail.position);
          camera.setFocalPoint(...detail.focalPoint);
          camera.setViewUp(...detail.viewUp);
          camera.setParallelScale(detail.parallelScale);
          renderWindow.render();
          applyingExternalCamera = false;
        };
        if (eventName) {
          window.addEventListener(eventName, receive);
          register(receive, (listener) => window.removeEventListener(eventName, listener));
        }
        const resize = () => view.resize();
        window.addEventListener('resize', resize);
        register(resize, (listener) => window.removeEventListener('resize', listener));

        setState('ready');
      } catch (error) {
        dispose(); // 初始化/请求/解析失败与卸载共用同一清理路径
        if (!cancelled) {
          setState('error');
          setMessage(error instanceof Error ? error.message : '无法加载三维预览。');
        }
      }
    })();

    return () => {
      cancelled = true;
      dispose();
    };
  }, [assets, cameraGroup, signature, sourceId]);

  return (
    <div className={styles.canvasWrap}>
      <div ref={containerRef} className={styles.canvas} />
      {state !== 'ready' ? <div className={styles.canvasMessage} data-error={state === 'error'}>{message}</div> : null}
    </div>
  );
}
