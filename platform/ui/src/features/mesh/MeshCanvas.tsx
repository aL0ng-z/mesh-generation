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
    let dispose = () => undefined;
    setState('loading');
    setMessage('正在加载三维运行时与预览资产…');

    void (async () => {
      try {
        const vtk = await loadVtkRuntime();
        if (cancelled) return;
        const view = vtk.GenericRenderWindow.newInstance({ background: [0.035, 0.065, 0.09] });
        view.setContainer(container);
        view.resize();
        const renderer = view.getRenderer();
        const renderWindow = view.getRenderWindow();
        const vtkObjects: any[] = [view];

        const responses = await Promise.all(assets.map(async (asset) => {
          const response = await fetch(asset.url, { headers: { Accept: 'application/vnd.vtk.vtp+xml, application/xml' } });
          if (!response.ok) throw new Error(`预览资产请求失败（HTTP ${response.status}）`);
          return response.arrayBuffer();
        }));
        if (cancelled) {
          view.delete();
          return;
        }

        responses.forEach((buffer, index) => {
          const reader = vtk.XMLPolyDataReader.newInstance();
          reader.parseAsArrayBuffer(buffer);
          const mapper = vtk.Mapper.newInstance();
          mapper.setInputConnection(reader.getOutputPort());
          const actor = vtk.Actor.newInstance();
          actor.setMapper(mapper);
          const color = colors[index % colors.length] ?? colors[0]!;
          actor.getProperty().setColor(...color);
          actor.getProperty().setOpacity(assets[index]?.wireframe ? 0.92 : 0.88);
          if (assets[index]?.wireframe) actor.getProperty().setLineWidth(1.25);
          renderer.addActor(actor);
          vtkObjects.push(actor, mapper, reader);
        });

        renderer.resetCamera();
        renderWindow.render();
        const camera = renderer.getActiveCamera();
        let applyingExternalCamera = false;
        const eventName = cameraGroup ? `mesh-camera:${cameraGroup}` : '';
        const publish = camera.onModified(() => {
          if (!eventName || applyingExternalCamera) return;
          const detail: CameraState = {
            source: sourceId,
            position: [...camera.getPosition()],
            focalPoint: [...camera.getFocalPoint()],
            viewUp: [...camera.getViewUp()],
            parallelScale: camera.getParallelScale(),
          };
          window.dispatchEvent(new CustomEvent(eventName, { detail }));
        });
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
        if (eventName) window.addEventListener(eventName, receive);
        const resize = () => view.resize();
        window.addEventListener('resize', resize);

        dispose = () => {
          if (eventName) window.removeEventListener(eventName, receive);
          window.removeEventListener('resize', resize);
          publish.unsubscribe();
          [...vtkObjects].reverse().forEach((object) => object.delete?.());
        };
        setState('ready');
      } catch (error) {
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
