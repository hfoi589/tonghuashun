import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendRoot = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(frontendRoot, '../vibe_research/desktop/src/verticals/finance'),
      '@ths': path.resolve(frontendRoot, 'src'),
      'react': path.resolve(frontendRoot, 'node_modules/react'),
      'react-dom': path.resolve(frontendRoot, 'node_modules/react-dom'),
      'react-router-dom': path.resolve(frontendRoot, 'node_modules/react-router-dom'),
      'lucide-react': path.resolve(frontendRoot, 'node_modules/lucide-react'),
      'react-markdown': path.resolve(frontendRoot, 'node_modules/react-markdown'),
      'remark-gfm': path.resolve(frontendRoot, 'node_modules/remark-gfm'),
      'clsx': path.resolve(frontendRoot, 'node_modules/clsx'),
      'tailwind-merge': path.resolve(frontendRoot, 'node_modules/tailwind-merge'),
      'zustand': path.resolve(frontendRoot, 'node_modules/zustand'),
      'echarts': path.resolve(frontendRoot, 'node_modules/echarts'),
    },
    dedupe: ['react', 'react-dom'],
  },
  test: {
    environment: 'jsdom',
    environmentOptions: {
      jsdom: { url: 'http://localhost/' },
    },
    setupFiles: './src/test/setup.ts',
    css: true,
  },
})
