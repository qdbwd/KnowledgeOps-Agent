---
name: frontend-design
description: Create distinctive, production-grade frontend interfaces with high design quality. Use this skill when the user asks to build web components, pages, artifacts, posters, or applications (examples include websites, landing pages, dashboards, React components, HTML/CSS layouts, or when styling/beautifying any web UI). Generates creative, polished code and UI design that avoids generic AI aesthetics.
license: Complete terms in LICENSE.txt
---

## KnowledgeOps 运行约定（2026-10-13 适配）

本包的方法来自 DeerFlow，执行环境为 KnowledgeOps。先遵循用户本次的目标、范围、详略和已有工具许可；下文的默认篇幅、搜索轮次及模板是参考，不得扩大简单任务。只使用本轮实际提供的工具和技能，不安装其他技能或依赖，不启用付费服务，不自动发布或发送内容。

- 联网使用 `search_web` 发现来源、`read_web` 读取原文，再按其返回的固定来源链接引用。关闭联网时不通过脚本绕过，可使用用户提供的材料并说明时效边界。
- 文件工具以挂载工作区为虚拟根：材料在 `/work` 或已登记的上传位置，产物写入 `/outputs`，通过 `present_files` 取得真实下载地址。知识正文编辑与发布仍遵循本应用规则。
- 技能正文和附属文本用 `read_file("/skills/<name>/...")` 按需读取；只有提供了 `execute` 时才可运行脚本。命令中使用 `python "$SKILLS_DIR/<name>/scripts/<file>.py"`，工作文件使用 `work/...`、`outputs/...` 相对路径。不要把文件工具的虚拟绝对路径直接当成 shell 路径。
- 当前日期采用运行上下文给出的日期；模板中的历史日期和示例数字不能当作事实。若缺少决定研究范围的关键条件，先提出简短澄清或说明有界假设。


This skill guides creation of distinctive, production-grade frontend interfaces that avoid generic "AI slop" aesthetics. Implement real working code with exceptional attention to aesthetic details and creative choices.

The user provides frontend requirements: a component, page, application, or interface to build. They may include context about the purpose, audience, or technical constraints.

## Output Requirements

For a new standalone HTML deliverable, use `index.html`; when editing an existing project, preserve its entry point and conventions.

## Design Thinking

Before coding, understand the context and commit to a BOLD aesthetic direction:
- **Purpose**: What problem does this interface solve? Who uses it?
- **Tone**: Pick an extreme: brutally minimal, maximalist chaos, retro-futuristic, organic/natural, luxury/refined, playful/toy-like, editorial/magazine, brutalist/raw, art deco/geometric, soft/pastel, industrial/utilitarian, etc. There are so many flavors to choose from. Use these for inspiration but design one that is true to the aesthetic direction.
- **Constraints**: Technical requirements (framework, performance, accessibility).
- **Differentiation**: What makes this UNFORGETTABLE? What's the one thing someone will remember?

**CRITICAL**: Choose a clear conceptual direction and execute it with precision. Bold maximalism and refined minimalism both work - the key is intentionality, not intensity.

Then implement working code (HTML/CSS/JS, React, Vue, etc.) that is:
- Production-grade and functional
- Visually striking and memorable
- Cohesive with a clear aesthetic point-of-view
- Meticulously refined in every detail

## Frontend Aesthetics Guidelines

Focus on:
- **Typography**: Choose fonts that are beautiful, unique, and interesting. Avoid generic fonts like Arial and Inter; opt instead for distinctive choices that elevate the frontend's aesthetics; unexpected, characterful font choices. Pair a distinctive display font with a refined body font.
- **Color & Theme**: Commit to a cohesive aesthetic. Use CSS variables for consistency. Dominant colors with sharp accents outperform timid, evenly-distributed palettes.
- **Motion**: Use animations for effects and micro-interactions. Prioritize CSS-only solutions for HTML. Use Motion library for React when available. Focus on high-impact moments: one well-orchestrated page load with staggered reveals (animation-delay) creates more delight than scattered micro-interactions. Use scroll-triggering and hover states that surprise.
- **Spatial Composition**: Unexpected layouts. Asymmetry. Overlap. Diagonal flow. Grid-breaking elements. Generous negative space OR controlled density.
- **Backgrounds & Visual Details**: Create atmosphere and depth rather than defaulting to solid colors. Add contextual effects and textures that match the overall aesthetic. Apply creative forms like gradient meshes, noise textures, geometric patterns, layered transparencies, dramatic shadows, decorative borders, custom cursors, and grain overlays.

NEVER use generic AI-generated aesthetics like overused font families (Inter, Roboto, Arial, system fonts), cliched color schemes (particularly purple gradients on white backgrounds), predictable layouts and component patterns, and cookie-cutter design that lacks context-specific character.

Interpret creatively and make unexpected choices that feel genuinely designed for the context. No design should be the same. Vary between light and dark themes, different fonts, different aesthetics. NEVER converge on common choices (Space Grotesk, for example) across generations.

**IMPORTANT**: Match implementation complexity to the aesthetic vision. Maximalist designs need elaborate code with extensive animations and effects. Minimalist or refined designs need restraint, precision, and careful attention to spacing, typography, and subtle details. Elegance comes from executing the vision well.

## Project conventions

Respect the user's existing visual identity and framework. Do not add a DeerFlow badge or any unrelated product signature. Use index.html for a standalone HTML artifact; existing applications retain their own entry point. Prefer the established design system when modifying existing pages.
