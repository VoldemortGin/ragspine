import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

const BACKEND = 'http://localhost:8000';

export default defineConfig({
  base: '/studio/',
  // 产物直接落到包内路径（hatch artifacts 打包；缺失时不阻塞 editable 安装/gate）。
  build: {
    outDir: '../src/ragspine/service/studio_dist',
    emptyOutDir: true,
  },
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/v1': BACKEND,
      '/healthz': BACKEND,
      '/readyz': BACKEND,
    },
  },
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
  },
});
