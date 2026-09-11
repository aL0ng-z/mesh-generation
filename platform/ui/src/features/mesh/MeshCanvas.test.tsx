import { act, render, screen, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MeshCanvas, type MeshAsset } from './MeshCanvas';
import { loadVtkRuntime, type VtkRuntime } from './vtkRuntime';

vi.mock('./vtkRuntime', () => ({ loadVtkRuntime: vi.fn() }));

const asset = (key: string): MeshAsset => ({ key, url: `https://mesh.test/${key}.vtp` });

function okResponse(buffer = new ArrayBuffer(8)) {
  return { ok: true, status: 200, arrayBuffer: async () => buffer };
}

function badResponse(status: number) {
  return { ok: false, status, arrayBuffer: async () => new ArrayBuffer(0) };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

interface FetchInit {
  headers?: Record<string, string>;
  signal: AbortSignal;
}

/** 安装 fetch mock，并把每次调用收到的 AbortSignal 收集起来供断言。 */
function stubFetch(handler: (url: string, init: FetchInit) => Promise<unknown>) {
  const fetchMock = vi.fn(handler);
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** 构造可断言的 vtk 运行时 mock：所有删除动作写入共享日志。 */
function createVtkRuntime() {
  const log: string[] = [];
  const views: Array<{ view: any; renderer: any; renderWindow: any }> = [];
  const readers: any[] = [];
  const mappers: any[] = [];
  const actors: any[] = [];

  const camera = {
    onModified: vi.fn(() => ({ unsubscribe: vi.fn(() => log.push('unsubscribe')) })),
    getPosition: vi.fn(() => [0, 0, 0]),
    getFocalPoint: vi.fn(() => [0, 0, 1]),
    getViewUp: vi.fn(() => [0, 1, 0]),
    getParallelScale: vi.fn(() => 1),
    setPosition: vi.fn(),
    setFocalPoint: vi.fn(),
    setViewUp: vi.fn(),
    setParallelScale: vi.fn(),
  };

  const runtime = {
    GenericRenderWindow: {
      newInstance: vi.fn(() => {
        const index = views.length;
        const renderWindow = {
          render: vi.fn(),
          delete: vi.fn(() => log.push(`delete:renderWindow:${index}`)),
        };
        const renderer = {
          resetCamera: vi.fn(),
          addActor: vi.fn(),
          getActiveCamera: vi.fn(() => camera),
          delete: vi.fn(() => log.push(`delete:renderer:${index}`)),
        };
        const view = {
          setContainer: vi.fn(),
          resize: vi.fn(),
          getRenderer: vi.fn(() => renderer),
          getRenderWindow: vi.fn(() => renderWindow),
          delete: vi.fn(() => log.push(`delete:view:${index}`)),
        };
        views.push({ view, renderer, renderWindow });
        return view;
      }),
    },
    XMLPolyDataReader: {
      newInstance: vi.fn(() => {
        const index = readers.length;
        const reader = {
          parseAsArrayBuffer: vi.fn(),
          getOutputPort: vi.fn(() => ({ __outputPort: index })),
          delete: vi.fn(() => log.push(`delete:reader:${index}`)),
        };
        readers.push(reader);
        return reader;
      }),
    },
    Mapper: {
      newInstance: vi.fn(() => {
        const index = mappers.length;
        const mapper = {
          setInputConnection: vi.fn(),
          delete: vi.fn(() => log.push(`delete:mapper:${index}`)),
        };
        mappers.push(mapper);
        return mapper;
      }),
    },
    Actor: {
      newInstance: vi.fn(() => {
        const index = actors.length;
        const actor = {
          setMapper: vi.fn(),
          getProperty: vi.fn(() => ({ setColor: vi.fn(), setOpacity: vi.fn(), setLineWidth: vi.fn() })),
          delete: vi.fn(() => log.push(`delete:actor:${index}`)),
        };
        actors.push(actor);
        return actor;
      }),
    },
  };

  return { runtime, views, readers, mappers, actors, camera, log };
}

beforeEach(() => {
  vi.mocked(loadVtkRuntime).mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('MeshCanvas 资源生命周期', () => {
  it('正常路径创建全部资源并渲染，卸载时按依赖逆序释放', async () => {
    const vtk = createVtkRuntime();
    vi.mocked(loadVtkRuntime).mockResolvedValue(vtk.runtime as unknown as VtkRuntime);
    const signals: AbortSignal[] = [];
    const buffer = new ArrayBuffer(8);
    const fetchMock = stubFetch(async (url, init) => {
      signals.push(init.signal);
      expect(init?.headers).toEqual({ Accept: 'application/vnd.vtk.vtp+xml, application/xml' });
      expect(url).toBe('https://mesh.test/b1.vtp');
      return okResponse(buffer);
    });
    const removeSpy = vi.spyOn(window, 'removeEventListener')
      .mockImplementation((type: string) => { vtk.log.push(`remove:${type}`); });

    const { unmount } = render(<MeshCanvas assets={[asset('b1')]} cameraGroup="grp" />);
    await waitFor(() => expect(screen.queryByText('正在加载三维运行时与预览资产…')).not.toBeInTheDocument());

    expect(vtk.runtime.GenericRenderWindow.newInstance).toHaveBeenCalledWith({ background: [0.035, 0.065, 0.09] });
    const { view, renderer, renderWindow } = vtk.views[0]!;
    expect(view.setContainer).toHaveBeenCalledWith(expect.any(HTMLDivElement));
    expect(view.resize).toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(signals[0]).toBeInstanceOf(AbortSignal);
    expect(vtk.readers[0]!.parseAsArrayBuffer).toHaveBeenCalledWith(buffer);
    expect(vtk.mappers[0]!.setInputConnection).toHaveBeenCalledWith(vtk.readers[0]!.getOutputPort());
    expect(vtk.actors[0]!.setMapper).toHaveBeenCalledWith(vtk.mappers[0]);
    expect(renderer.addActor).toHaveBeenCalledWith(vtk.actors[0]);
    expect(renderer.resetCamera).toHaveBeenCalled();
    expect(renderWindow.render).toHaveBeenCalledTimes(1);
    expect(vtk.camera.onModified).toHaveBeenCalledTimes(1);

    // 相机同步：外部 detail 应用到本相机并触发重绘。
    act(() => {
      window.dispatchEvent(new CustomEvent('mesh-camera:grp', {
        detail: { source: 'other', position: [1, 2, 3], focalPoint: [0, 0, 1], viewUp: [0, 1, 0], parallelScale: 4 },
      }));
    });
    expect(vtk.camera.setPosition).toHaveBeenCalledWith(1, 2, 3);
    expect(vtk.camera.setFocalPoint).toHaveBeenCalledWith(0, 0, 1);
    expect(renderWindow.render).toHaveBeenCalledTimes(2);
    expect(view.delete).not.toHaveBeenCalled();

    unmount();
    // 后创建先释放：监听器 → 订阅 → actor → mapper → reader → renderWindow → renderer → view。
    expect(vtk.log).toEqual([
      'remove:resize',
      'remove:mesh-camera:grp',
      'unsubscribe',
      'delete:actor:0',
      'delete:mapper:0',
      'delete:reader:0',
      'delete:renderWindow:0',
      'delete:renderer:0',
      'delete:view:0',
    ]);
    expect(removeSpy).toHaveBeenCalledWith('resize', expect.any(Function));
    expect(signals[0]!.aborted).toBe(true);
    expect(view.delete).toHaveBeenCalledTimes(1);
    expect(renderer.delete).toHaveBeenCalledTimes(1);
    expect(renderWindow.delete).toHaveBeenCalledTimes(1);
  });

  it('单个资产请求失败时清理已创建资源并取消同批剩余请求', async () => {
    const vtk = createVtkRuntime();
    vi.mocked(loadVtkRuntime).mockResolvedValue(vtk.runtime as unknown as VtkRuntime);
    const signals: AbortSignal[] = [];
    const fetchMock = stubFetch((url, init) => {
      signals.push(init.signal);
      if (url.includes('b1')) return Promise.resolve(badResponse(401));
      // 同批第二个请求挂起，直到被 abort 取消。
      return new Promise((resolve, reject) => {
        init.signal.addEventListener('abort', () => reject(new DOMException('已中止', 'AbortError')));
        resolve(badResponse(500));
      });
    });

    const { unmount } = render(<MeshCanvas assets={[asset('b1'), asset('b2')]} />);
    await screen.findByText('预览资产请求失败（HTTP 401）');

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(signals[0]!.aborted).toBe(true); // 第一个请求失败后同批共享的 controller 被中止
    expect(vtk.readers).toHaveLength(0); // 请求失败时尚未创建 reader
    expect(vtk.camera.onModified).not.toHaveBeenCalled();
    const { view, renderer, renderWindow } = vtk.views[0]!;
    expect(view.delete).toHaveBeenCalledTimes(1);
    expect(renderer.delete).toHaveBeenCalledTimes(1);
    expect(renderWindow.delete).toHaveBeenCalledTimes(1);
    expect(vtk.log).toEqual(['delete:renderWindow:0', 'delete:renderer:0', 'delete:view:0']);

    unmount(); // 再次清理保持幂等，不重复释放
    expect(view.delete).toHaveBeenCalledTimes(1);
    expect(renderer.delete).toHaveBeenCalledTimes(1);
  });

  it('卸载时中止在途请求，迟到结果不创建资源也不更新组件', async () => {
    const vtk = createVtkRuntime();
    vi.mocked(loadVtkRuntime).mockResolvedValue(vtk.runtime as unknown as VtkRuntime);
    const signals: AbortSignal[] = [];
    const pending = deferred<unknown>();
    stubFetch((_url, init) => {
      signals.push(init.signal);
      return pending.promise;
    });

    const { unmount } = render(<MeshCanvas assets={[asset('b1')]} />);
    await waitFor(() => expect(vtk.views).toHaveLength(1));
    expect(signals[0]!.aborted).toBe(false);

    unmount();
    expect(signals[0]!.aborted).toBe(true); // cleanup 中止在途请求
    expect(vtk.views[0]!.view.delete).toHaveBeenCalledTimes(1);

    // 请求迟到返回：结果被丢弃，不创建 reader/actor，不触发任何状态更新。
    await act(async () => {
      pending.resolve(okResponse());
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(vtk.readers).toHaveLength(0);
    expect(vtk.mappers).toHaveLength(0);
    expect(vtk.actors).toHaveLength(0);
    expect(vtk.views[0]!.renderer.addActor).not.toHaveBeenCalled();
    expect(vtk.views[0]!.view.delete).toHaveBeenCalledTimes(1);
  });

  it('运行时在卸载后才就绪时不创建任何资源', async () => {
    const vtk = createVtkRuntime();
    const pending = deferred<unknown>();
    vi.mocked(loadVtkRuntime).mockReturnValue(pending.promise as Promise<VtkRuntime>);
    const fetchMock = stubFetch(async () => okResponse());

    const { unmount } = render(<MeshCanvas assets={[asset('b1')]} />);
    unmount();

    await act(async () => {
      pending.resolve(vtk.runtime);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(vtk.runtime.GenericRenderWindow.newInstance).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('解析失败路径同样清理全部已创建资源', async () => {
    const vtk = createVtkRuntime();
    vi.mocked(loadVtkRuntime).mockResolvedValue(vtk.runtime as unknown as VtkRuntime);
    const signals: AbortSignal[] = [];
    stubFetch((_url, init) => {
      signals.push(init.signal);
      return Promise.resolve(okResponse());
    });
    const throwingReader = {
      parseAsArrayBuffer: vi.fn(() => { throw new Error('VTP 解析失败'); }),
      getOutputPort: vi.fn(),
      delete: vi.fn(),
    };
    vtk.runtime.XMLPolyDataReader.newInstance.mockImplementationOnce(() => throwingReader);

    const { unmount } = render(<MeshCanvas assets={[asset('b1')]} />);
    await screen.findByText('VTP 解析失败');

    // reader 在解析前已登记，解析抛错后同样被释放。
    expect(throwingReader.delete).toHaveBeenCalledTimes(1);
    expect(vtk.runtime.Mapper.newInstance).not.toHaveBeenCalled();
    const { view, renderer, renderWindow } = vtk.views[0]!;
    expect(view.delete).toHaveBeenCalledTimes(1);
    expect(renderer.delete).toHaveBeenCalledTimes(1);
    expect(renderWindow.delete).toHaveBeenCalledTimes(1);
    expect(signals[0]!.aborted).toBe(true);

    unmount(); // 幂等：不重复释放
    expect(throwingReader.delete).toHaveBeenCalledTimes(1);
    expect(view.delete).toHaveBeenCalledTimes(1);
  });

  it('StrictMode 双执行安全：首次执行的迟到结果被丢弃，仅一次完整初始化', async () => {
    const vtk = createVtkRuntime();
    vi.mocked(loadVtkRuntime).mockResolvedValue(vtk.runtime as unknown as VtkRuntime);
    const signals: AbortSignal[] = [];
    const fetchMock = stubFetch((_url, init) => {
      signals.push(init.signal);
      return Promise.resolve(okResponse());
    });

    const { unmount } = render(
      <StrictMode>
        <MeshCanvas assets={[asset('b1')]} />
      </StrictMode>,
    );
    await waitFor(() => expect(screen.queryByText('正在加载三维运行时与预览资产…')).not.toBeInTheDocument());

    // 第一次 effect 在运行时就绪前已被 cleanup 取消，只有第二次完整初始化。
    expect(vtk.views).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(signals[0]!.aborted).toBe(false);
    expect(vtk.views[0]!.view.delete).not.toHaveBeenCalled();

    unmount();
    expect(vtk.views[0]!.view.delete).toHaveBeenCalledTimes(1);
    expect(signals[0]!.aborted).toBe(true);
  });
});
