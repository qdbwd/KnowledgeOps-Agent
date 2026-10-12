import { defineConfig } from '@playwright/test';

// 两个进程都在本项目开发容器内。API 使用 knowledgeops_test 的随机 schema。
export default defineConfig({
  testDir: './tests',
  workers: 1,
  retries: 0,
  timeout: 30000,
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  reporter: [
    ['list'],
    [
      'json',
      {
        outputFile:
          process.env.KNOWLEDGEOPS_BROWSER_REPORT ??
          '../artifacts/verification/browser-results.json',
      },
    ],
  ],
  webServer: [
    {
      command: 'cd /workspace && uv run --locked python tests/browser_server.py',
      url: 'http://127.0.0.1:8001/api/health',
      reuseExistingServer: false,
      // Windows 源码挂载下冷导入并初始化隔离数据库，预留启动时间而非放宽断言。
      timeout: 120000,
      gracefulShutdown: { signal: 'SIGTERM', timeout: 10000 },
    },
    {
      command: 'API_PROXY_TARGET=http://127.0.0.1:8001 npm run dev -- --port 4173',
      url: 'http://127.0.0.1:4173',
      reuseExistingServer: false,
      timeout: 30000,
      gracefulShutdown: { signal: 'SIGTERM', timeout: 5000 },
    },
  ],
});
