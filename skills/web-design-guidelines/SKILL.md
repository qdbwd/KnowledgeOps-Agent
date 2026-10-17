---
name: web-design-guidelines
description: Review UI code for Web Interface Guidelines compliance. Use when asked to "review my UI", "check accessibility", "audit design", "review UX", or "check my site against best practices".
metadata:
  author: vercel
  version: "1.0.0"
  argument-hint: <file-or-pattern>
---

## KnowledgeOps 运行约定（2026-10-13 适配）

本包的方法来自 DeerFlow，执行环境为 KnowledgeOps。先遵循用户本次的目标、范围、详略和已有工具许可；下文的默认篇幅、搜索轮次及模板是参考，不得扩大简单任务。只使用本轮实际提供的工具和技能，不安装其他技能或依赖，不启用付费服务，不自动发布或发送内容。

- 联网使用 `search_web` 发现来源、`read_web` 读取原文，再按其返回的固定来源链接引用。关闭联网时不通过脚本绕过，可使用用户提供的材料并说明时效边界。
- 文件工具以挂载工作区为虚拟根：材料在 `/work` 或已登记的上传位置，产物写入 `/outputs`，通过 `present_files` 取得真实下载地址。知识正文编辑与发布仍遵循本应用规则。
- 技能正文和附属文本用 `read_file("/skills/<name>/...")` 按需读取；只有提供了 `execute` 时才可运行脚本。命令中使用 `python "$SKILLS_DIR/<name>/scripts/<file>.py"`，工作文件使用 `work/...`、`outputs/...` 相对路径。不要把文件工具的虚拟绝对路径直接当成 shell 路径。
- 当前日期采用运行上下文给出的日期；模板中的历史日期和示例数字不能当作事实。若缺少决定研究范围的关键条件，先提出简短澄清或说明有界假设。


# Web Interface Guidelines

Review files for compliance with Web Interface Guidelines.

## How It Works

1. Fetch the latest guidelines from the source URL below
2. Read the specified files (or prompt user for files/pattern)
3. Check against all rules in the fetched guidelines
4. Output findings in the terse `file:line` format

## Guidelines Source

Fetch fresh guidelines before each review:

```
https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md
```

Use read_web to retrieve the latest rules. The fetched content contains all the rules and output format instructions.

## Usage

When a user provides a file or pattern argument:
1. Fetch guidelines from the source URL above
2. Read the specified files
3. Apply all rules from the fetched guidelines
4. Output findings using the format specified in the guidelines

If no files specified, ask the user which files to review.
