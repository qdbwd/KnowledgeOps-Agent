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
│   ├── sources/           材料正文、会话保存意图、固定交付及资产删除
│   ├── runs/              任务摘要/控制、独立诊断页、子任务及产物入口
│   ├── learning/          学习任务与两层配置
│   ├── memory/            长期记忆、动态类别管理及后台分类状态
│   ├── permissions/       执行审批、未知结果和命令规则
│   ├── tools/             工具与技能管理、按需正文/参数详情、工具组及共用能力选择器
│   ├── mcp/               MCP 候选、依赖准备、凭据、测试授权及移除清理
│   ├── skills/            完整技能包导入/试读、启停、更新、导出和移除
│   └── subagents/         角色列表、独立创建/编辑页、共用字段与模板选择
├── shared/
│   ├── api/               HTTP 请求、分页与加载 Hook
│   ├── types/             多页面共用的业务类型
│   ├── state/             工作区刷新通知
│   └── ui/                Markdown、状态、错误、加载及原生模态框
└── styles/                Tailwind 入口、主题、基础控件和正文排版
    ├── styles.css         入口与扫描排除
    ├── theme.css          配色、字体、响应式断点
    ├── base.css           Preflight 之后的基础排版与表单默认值
    ├── controls.css       Button 与按钮式链接的共用样式
    └── content.css        Markdown、源码行号与材料文本
```

`app/App.tsx` 把 URL 映射到功能页面；页面通过 `shared/api` 调用后端，复用 `shared/ui` 展示结果。组件专属于一个功能时，继续放在对应 `features/` 下。只有多个功能实际共用的基础能力才进入 `shared/`，共享层不反向导入页面。

知识库的文章与审阅入口分别为`/knowledge`、`/knowledge/reviews`，旧`/changes`地址保留重定向；运行记录独立在`/runs`。聊天删除使用确认弹窗，受理后回到新对话，布局级监视器只在后台清理失败时提示；最近会话只显示已有真人消息的会话，右键/手机菜单支持重命名。内部材料归属ID不等于已经开始一段聊天。

组件测试与被测功能放在一起；跨页面的集合行为测试在 `app/`；真实浏览器场景仍在 `tests/`。目录调整不更改页面地址、视觉风格和用户操作流程，也没有引入新状态管理框架。

会话目录与公开正文检索入口为`/conversations`；聊天通过`useMessageHistory`读取有展示修订的消息窗口，旧游标失效后重新定位可见消息。`chat/drafts`分别负责浏览器IndexedDB事务、编辑缓冲/跨标签通知和创建/发送回执核对。网络请求与本地事务分开；不要恢复成刷新后自动重新POST，或发送成功后无条件清空整份输入。旧`composerDraft.ts`仅保留sessionStorage迁入读取。草稿状态通过`useSyncExternalStore`接入React，组件测试使用fake-indexeddb，真实持久性和竞争仍由浏览器场景验证。

前端沿用适合本项目的功能分组，没有照搬 DeerFlow 的 Next.js 路由目录。与后端采用相同的原则：应用装配、功能代码和共用基础各有位置，目录深度以查找方便为准。

## 样式维护

Tailwind 通过官方 `@tailwindcss/vite` 插件参与开发与生产构建，依赖和版本固定在 `package.json`、`package-lock.json`；没有新增运行服务。布局、间距、响应式和局部状态直接放在组件的 `className` 中。原有 `card`、`composer` 等语义标记继续用于浏览器定位或局部选择器，不再各自依赖一套全局 CSS 布局。

颜色优先使用 `bg-brand`、`text-muted`、`border-line` 等主题工具；仅局部使用的既有细节色保留明确值。按钮使用 `shared/ui/Button.tsx`，链接可使用相同的 `button`、`button-secondary`、`button-quiet` 配方。页面上的尺寸工具可以覆盖配方，不靠提高选择器优先级或追加全局覆盖。

字号工具不自动改变行距，正文继续使用 1.9、表单使用各自行距。Markdown 是运行时材料，保留 `.markdown` 作用域的标题、列表、引用、表格和代码样式；源码阅读器也单独保留行号与定位规则。不要为减少 CSS 行数而破坏完整正文排版。

动态状态必须选择完整、可静态扫描的类名。例如 Badge 在几个固定颜色组合中选择，不拼接 `bg-${color}`。新增样式沿用 760px/1100px 的 `md`/`lg` 断点。入口排除组件测试，避免夹具里的类名进入产品样式。

在本项目 dev 容器的 `/workspace/frontend` 执行 `npm run build`、`npm run lint`、`npm test`。浏览器回归沿用隔离服务，覆盖聊天底栏、全文阅读和各管理页桌面/窄屏布局；见 `tests/chat-reader.spec.ts`、`tests/layout.spec.ts`、`tests/styles.spec.ts`。参考：[Vite 官方接入](https://tailwindcss.com/docs/installation/using-vite)、[主题变量](https://tailwindcss.com/docs/theme)、[Preflight](https://tailwindcss.com/docs/preflight)。
