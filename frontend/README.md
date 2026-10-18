# 前端源码导航

前端使用 React、TypeScript、Vite 和 Tailwind CSS v4，保留客户端路由的 SPA 架构。启动、环境和依赖安装由[项目 README](../README.md)中的 Docker 工作流统一说明。

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
└── styles/                Tailwind 入口、主题、基础控件和正文排版
    ├── styles.css         入口与扫描排除
    ├── theme.css          配色、字体、响应式断点
    ├── base.css           Preflight 之后的基础排版与表单默认值
    ├── controls.css       Button 与按钮式链接的共用样式
    └── content.css        Markdown、源码行号与材料文本
```

`app/App.tsx` 把 URL 映射到功能页面；页面通过 `shared/api` 调用后端，复用 `shared/ui` 展示结果。组件专属于一个功能时，继续放在对应 `features/` 下。只有多个功能实际共用的基础能力才进入 `shared/`，共享层不反向导入页面。

组件测试与被测功能放在一起；跨页面的集合行为测试在 `app/`；真实浏览器场景仍在 `tests/`。目录调整不更改页面地址、视觉风格和用户操作流程，也没有引入新状态管理框架。

前端沿用适合本项目的功能分组，没有照搬 DeerFlow 的 Next.js 路由目录。与后端采用相同的原则：应用装配、功能代码和共用基础各有位置，目录深度以查找方便为准。

## 样式维护

Tailwind 通过官方 `@tailwindcss/vite` 插件参与开发与生产构建，依赖和版本固定在 `package.json`、`package-lock.json`；没有新增运行服务。布局、间距、响应式和局部状态直接放在组件的 `className` 中。原有 `card`、`composer` 等语义标记继续用于浏览器定位或局部选择器，不再各自依赖一套全局 CSS 布局。

颜色优先使用 `bg-brand`、`text-muted`、`border-line` 等主题工具；仅局部使用的既有细节色保留明确值。按钮使用 `shared/ui/Button.tsx`，链接可使用相同的 `button`、`button-secondary`、`button-quiet` 配方。页面上的尺寸工具可以覆盖配方，不靠提高选择器优先级或追加全局覆盖。

字号工具不自动改变行距，正文继续使用 1.9、表单使用各自行距。Markdown 是运行时材料，保留 `.markdown` 作用域的标题、列表、引用、表格和代码样式；源码阅读器也单独保留行号与定位规则。不要为减少 CSS 行数而破坏完整正文排版。

动态状态必须选择完整、可静态扫描的类名。例如 Badge 在几个固定颜色组合中选择，不拼接 `bg-${color}`。新增样式沿用 760px/1100px 的 `md`/`lg` 断点。入口排除组件测试，避免夹具里的类名进入产品样式。

在本项目 dev 容器的 `/workspace/frontend` 执行 `npm run build`、`npm run lint`、`npm test`。浏览器回归沿用隔离服务，覆盖聊天底栏、全文阅读和各管理页桌面/窄屏布局；见 `tests/chat-reader.spec.ts`、`tests/layout.spec.ts`、`tests/styles.spec.ts`。参考：[Vite 官方接入](https://tailwindcss.com/docs/installation/using-vite)、[主题变量](https://tailwindcss.com/docs/theme)、[Preflight](https://tailwindcss.com/docs/preflight)。
