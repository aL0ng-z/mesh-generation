/// <reference types="vite/client" />

declare module '*.module.css' {
  const classes: Readonly<Record<string, string>>;
  export default classes;
}

declare module '@kitware/vtk.js/Rendering/Profiles/Geometry';
