---
name: surprise-me
description: Create a delightful, unexpected "wow" experience for the user by dynamically discovering and creatively combining other enabled skills. Triggers when the user says "surprise me" or any request expressing a desire for an unexpected creative showcase. Also triggers when the user is bored, wants inspiration, or asks for "something interesting".
---

## KnowledgeOps 运行约定（2026-10-13 适配）

本包的方法来自 DeerFlow，执行环境为 KnowledgeOps。先遵循用户本次的目标、范围、详略和已有工具许可；下文的默认篇幅、搜索轮次及模板是参考，不得扩大简单任务。只使用本轮实际提供的工具和技能，不安装其他技能或依赖，不启用付费服务，不自动发布或发送内容。

- 联网使用 `search_web` 发现来源、`read_web` 读取原文，再按其返回的固定来源链接引用。关闭联网时不通过脚本绕过，可使用用户提供的材料并说明时效边界。
- 文件工具以挂载工作区为虚拟根：材料在 `/work` 或已登记的上传位置，产物写入 `/outputs`，通过 `present_files` 取得真实下载地址。知识正文编辑与发布仍遵循本应用规则。
- 技能正文和附属文本用 `read_file("/skills/<name>/...")` 按需读取；只有提供了 `execute` 时才可运行脚本。命令中使用 `python "$SKILLS_DIR/<name>/scripts/<file>.py"`，工作文件使用 `work/...`、`outputs/...` 相对路径。不要把文件工具的虚拟绝对路径直接当成 shell 路径。
- 当前日期采用运行上下文给出的日期；模板中的历史日期和示例数字不能当作事实。若缺少决定研究范围的关键条件，先提出简短澄清或说明有界假设。


# Surprise Me

Deliver an unexpected, delightful experience by dynamically discovering available skills and combining them creatively.

## Workflow

### Step 1: Discover Available Skills

Read the names and descriptions in the enabled skills list provided by the runtime. Only load the 1–3 relevant skill bodies.

### Step 2: Plan the Surprise

Select **1 to 3** skills and design a creative mashup. The goal is a single cohesive deliverable, not separate demos.

**Creative combination principles:**
- Juxtapose skills in unexpected ways (e.g., a presentation about algorithmic art, a research report turned into a slide deck, a styled doc with canvas-designed illustrations)
- Incorporate the user's known interests/context from memory if available
- Prioritize visual impact and emotional delight over information density
- The output should feel like a gift — polished, surprising, and fun

**Theme ideas (pick or remix):**
- Something tied to today's date, season, or trending news
- A mini creative project the user never asked for but would love
- A playful "what if" concept
- An aesthetic artifact combining data + design
- A fun interactive HTML/React experience

### Step 3: Fallback — No Other Skills Available

If no other skills are discovered (only surprise-me exists), use one of these fallbacks:

1. **News-based surprise**: Search today's news for a fascinating story, then create a beautifully designed HTML artifact presenting it in a visually striking way
2. **Interactive HTML experience**: Build a creative single-page web experience — generative art, a mini-game, a visual poem, an animated infographic, or an interactive story
3. **Personalized artifact**: Use known user context to create something personal and delightful

### Step 4: Execute

1. Read the full SKILL.md body of each selected skill
2. Follow each skill's instructions for technical execution
3. Combine outputs into one cohesive deliverable
4. Present the result with minimal preamble — let the work speak for itself

### Step 5: Reveal

Present the surprise with minimal spoilers. A short teaser line, then the artifact.

- **Good reveal:** "I made you something ✨" + [the artifact]
- **Bad reveal:** "I decided to combine the pptx skill with the canvas-design skill to create a presentation about..." (kills the surprise)
