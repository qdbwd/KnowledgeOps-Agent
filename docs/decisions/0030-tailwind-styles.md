# 0030：保留 SPA，使用 Tailwind 组织前端样式

2026-10-18，按用户确认实施。当前前端继续使用 React、TypeScript、Vite 和 React Router；这次迁移改变样式维护方式。开发说明见 [architecture](../architecture.md#tailwind-样式组织)，验证与运行状态见 [progress](../progress.md)。

原有单个样式文件混合了基础元素、页面布局和后续覆盖，定位一个控件需要来回检查多个规则。改用 Tailwind CSS 4.3.3 与官方 `@tailwindcss/vite` 插件，让布局、间距、响应式和局部状态与相应组件放在一起。主色、字体和断点集中到主题文件；共享按钮保留小型组件与统一样式配方。已有语义类可继续服务测试定位和局部关系选择，删除已替换的全局布局规则。

Markdown、源文本和代码行号继续使用有作用域的专用 CSS。它们来自运行时正文，适合按元素组织排版。Preflight 的重置会影响标题、列表、边距与表单，必须显式恢复这些默认值；字号工具与行距分开，避免聊天正文密度变化。动态状态选择完整静态类名，并让模板插值前后的类名以空格分隔，保证构建扫描能识别。

直接依赖的是 Tailwind 官方构建插件，没有复制 DeerFlow 的前端或引入新的 UI 组件库。参考：[Vite 接入](https://tailwindcss.com/docs/installation/using-vite)、[主题变量](https://tailwindcss.com/docs/theme)、[Preflight](https://tailwindcss.com/docs/preflight)。保持既有绿色风格、聊天底栏和完整阅读交互，验证使用组件检查、实际浏览器与迁移前后截图。

用户另行确认保留 SPA。当前主要功能是聊天、编辑与任务状态交互，由浏览器通过现有 HTTP/SSE 接口访问后端，部署仍输出由 Nginx 提供的静态文件。暂没有需要服务端渲染或静态生成的公开知识页面需求，所以本轮不迁移 Next.js。如果以后增加可公开索引的知识页，再单独评估按页面引入 SSR/SSG；Next.js 官方支持 [SPA 渐进采用服务端能力](https://nextjs.org/docs/app/guides/single-page-applications)，这一后续选项不构成本轮实施承诺。
