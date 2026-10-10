import { fileURLToPath, URL } from 'node:url'

import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Backend settings stay in the dev server; only VITE_* is exposed to browser code.
  const env = loadEnv(mode, fileURLToPath(new URL('../..', import.meta.url)), '')
  const target = process.env.INCIDENT_API_TARGET || env.INCIDENT_API_TARGET
    || `http://127.0.0.1:${env.PORT || '8003'}`
  return {
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    proxy: {
      '/api': {
        target,
        changeOrigin: true,
      },
      '/health': {
        target,
        changeOrigin: true,
      },
      '/docs': { target, changeOrigin: true },
      '/openapi.json': { target, changeOrigin: true },
    },
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    },
  },
  }
})
