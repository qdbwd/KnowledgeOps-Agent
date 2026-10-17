"""真实联网验证，不调用模型；来源与报告只保存到本次隔离空间。"""

import json
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.persistence.models import Base
from knowledgeops_agent.sources.service import SourceService
from knowledgeops_agent.sources.web import WebResearchService


def main():
    admin = create_engine(os.environ["TEST_DATABASE_URL"])
    assert admin.url.database == "knowledgeops_test"
    scope = "network_" + uuid4().hex
    output = Path("artifacts/verification") / scope
    output.mkdir(parents=True)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{scope}"'))
    engine = create_engine(admin.url, connect_args={"options": f"-csearch_path={scope}"})
    report = {
        "scope": scope,
        "kind": "real-network-no-model",
        "searches": [],
        "reads": [],
        "passed": False,
    }
    try:
        Base.metadata.create_all(engine)
        sources = SourceService(sessionmaker(engine, expire_on_commit=False), output / "originals")
        service = WebResearchService(sources)
        for query in ["Python context manager documentation", "Python 上下文管理器 官方文档 with"]:
            try:
                found = service.search(query, count=4)
                report["searches"].append({"query": query, "results": found})
            except ValueError as exc:
                report["searches"].append({"query": query, "error": str(exc)})
            print(
                {"searched": query, "count": len(report["searches"][-1].get("results", []))},
                flush=True,
            )
        for url in [
            "https://docs.python.org/3/library/contextlib.html",
            "https://docs.python.org/zh-cn/3/library/contextlib.html",
        ]:
            try:
                result = service.read(url, root_id=scope)
                cached = service.read(url, root_id=scope)
                report["reads"].append(
                    {"url": url, "result": result, "second_cached": cached["cached"]}
                )
            except ValueError as exc:
                report["reads"].append({"url": url, "error": str(exc)})
            print({"read": url, "success": "result" in report["reads"][-1]}, flush=True)
        report["passed"] = all(row.get("results") for row in report["searches"]) and all(
            row.get("second_cached") for row in report["reads"]
        )
    finally:
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 保留隔离 schema 与原件，报告中的引用之后仍可审查；不删除业务数据。
        engine.dispose()
        admin.dispose()
    print({"passed": report["passed"], "report": str(output / "report.json")}, flush=True)
    if not report["passed"]:
        raise RuntimeError("network verification needs investigation; see saved evidence")


if __name__ == "__main__":
    main()
