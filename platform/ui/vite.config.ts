import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

const xmlBuilderBrowserBundle = decodeURIComponent(
  new URL('./node_modules/xmlbuilder2/lib/xmlbuilder2.min.js', import.meta.url).pathname,
).replace(/^\/([A-Za-z]:\/)/, '$1');

export default defineConfig({
  plugins: [react()],
  resolve: {
    // vtk.js 的 XML reader 只需要 create()；浏览器 UMD 包避免 Vite 8
    // 把 xmlbuilder2 的 Node events/url 分支外置成 undefined。
    alias: { xmlbuilder2: xmlBuilderBrowserBundle },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    css: true,
  },
});
