import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

const BACKEND = 'http://localhost:8000';

export default defineConfig({
  base: '/studio/',
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
