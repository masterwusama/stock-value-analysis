import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期代理:Vue3(5173) → FastAPI(8000)
export default defineConfig({
  plugins: [vue()],
  server: {
    host: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
