# 前端源码导航

前端继续使用 React、TypeScript 和 Vite。启动、环境和依赖安装由[项目 README](../README.md)中的 Docker 工作流统一说明。

```text
src/
├── main.tsx               浏览器入口
├── app/                   路由、侧栏、应用装配与跨页面测试
├── features/              页面、专用组件及就近的组件测试
│   ├── chat/              聊天、上下文占用和引导消息
│   ├── knowledge/         知识目录与正文
│   ├── changes/           草稿与变更
│   ├── sources/           材料和证据
│   ├── runs/              执行过程、资源快照及产物
│   ├── learning/          学习任务与两层配置
│   ├── memory/            长期记忆和偏好
│   ├── permissions/       执行审批、未知结果和命令规则
│   ├── tools/             工具与技能管理
│   ├── mcp/               MCP 连接与审批展示
│   └── subagents/         子角色配置
├── shared/
│   ├── api/               HTTP 请求、分页与加载 Hook
│   ├── types/             多页面共用的业务类型
│   ├── state/             工作区刷新通知
│   └── ui/                Markdown、状态、错误、加载等共用展示
└── styles/                原有整体样式
```

`app/App.tsx` 把 URL 映射到功能页面；页面通过 `shared/api` 调用后端，复用 `shared/ui` 展示结果。组件专属于一个功能时，继续放在对应 `features/` 下。只有多个功能实际共用的基础能力才进入 `shared/`，共享层不反向导入页面。

组件测试与被测功能放在一起；跨页面的集合行为测试在 `app/`；真实浏览器场景仍在 `tests/`。目录调整不更改页面地址、视觉风格和用户操作流程，也没有引入新状态管理框架。

前端沿用适合本项目的功能分组，没有照搬 DeerFlow 的 Next.js 路由目录。与后端采用相同的原则：应用装配、功能代码和共用基础各有位置，目录深度以查找方便为准。
