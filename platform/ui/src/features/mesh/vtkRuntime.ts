export interface VtkRuntime {
  GenericRenderWindow: { newInstance: (...args: any[]) => any };
  XMLPolyDataReader: { newInstance: () => any };
  Mapper: { newInstance: () => any };
  Actor: { newInstance: () => any };
}

let runtimePromise: Promise<VtkRuntime> | undefined;

/** vtk.js 及 Geometry profile 只会进入 Viewer 异步 chunk，不污染首页首屏。 */
export function loadVtkRuntime(): Promise<VtkRuntime> {
  runtimePromise ??= Promise.all([
    import('@kitware/vtk.js/Rendering/Profiles/Geometry'),
    import('@kitware/vtk.js/Rendering/Misc/GenericRenderWindow'),
    import('@kitware/vtk.js/IO/XML/XMLPolyDataReader'),
    import('@kitware/vtk.js/Rendering/Core/Mapper'),
    import('@kitware/vtk.js/Rendering/Core/Actor'),
  ]).then(([, generic, reader, mapper, actor]) => ({
    GenericRenderWindow: generic.default,
    XMLPolyDataReader: reader.default,
    Mapper: mapper.default,
    Actor: actor.default,
  }) as VtkRuntime);
  return runtimePromise!;
}
