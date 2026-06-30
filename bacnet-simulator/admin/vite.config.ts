import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/devices': { target: 'http://localhost:47900', changeOrigin: true },
      '/health':  { target: 'http://localhost:47900', changeOrigin: true },
      '/meta':    { target: 'http://localhost:47900', changeOrigin: true },
      '/state':   { target: 'http://localhost:47900', changeOrigin: true },
      '/reload':  { target: 'http://localhost:47900', changeOrigin: true },
      '/ws':      { target: 'ws://localhost:47900', ws: true, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
