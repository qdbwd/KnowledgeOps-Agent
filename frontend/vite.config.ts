import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 开发页面与 API 同源；生产由 Nginx 转发，浏览器不接触容器服务名。
export default defineConfig({
  plugins: [react()],
  server: {
    watch: { usePolling: process.env.DEV_WATCH_POLLING === 'true', interval: 500 },
    proxy: {
      '/api': {
        target: process.env.API_PROXY_TARGET ?? 'http://127.0.0.1:8000',
        // 简写字符串会默认改写 Host，破坏后端基于 Origin/Host 的同源检查。
        changeOrigin: false,
      },
    },
  },
});
