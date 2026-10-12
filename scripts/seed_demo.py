"""导入公开可分享的合成演示材料，不创建知识正文、不调用模型或清空数据。

在 dev 中执行 python scripts/seed_demo.py。重复执行由来源服务内容去重，
随后用户在 Web 选中这些材料，真实尝试预览、发布、问答和维护。
"""

import json
from pathlib import Path

from knowledgeops_agent.config import Settings
from knowledgeops_agent.services import create_services


def main():
    services = create_services(Settings(model_calls_enabled=False))
    try:
        rows = []
        for path in sorted(Path("demo/materials").glob("*.md")):
            source = services.sources.import_text(
                path.stem, path.read_text(encoding="utf-8"), origin="demo:" + path.name
            )
            rows.append({"id": source["id"], "title": source["title"]})
        print(json.dumps({"sources": rows, "model_requests": 0}, ensure_ascii=False))
    finally:
        services.close()


if __name__ == "__main__":
    main()
