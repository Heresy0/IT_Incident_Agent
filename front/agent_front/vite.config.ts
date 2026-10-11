import { fileURLToPath, URL } from 'node:url'
import { existsSync, readFileSync } from 'node:fs'
import { parseEnv } from 'node:util'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig(() => {
  // Backend settings stay in the dev server; only VITE_* is exposed to browser code.
  const envPath = fileURLToPath(new URL('../../.env', import.meta.url))
  const env = existsSync(envPath) ? parseEnv(readFileSync(envPath, 'utf-8')) : {}
  const target = process.env.INCIDENT_API_TARGET || env.INCIDENT_API_TARGET
    || `http://127.0.0.1:${process.env.PORT || env.PORT || '8003'}`
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
