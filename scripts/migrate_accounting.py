"""模型回执库的显式增量迁移；在开发/维护容器中运行，绝不发送模型请求。"""

import argparse
import json

from sqlalchemy import create_engine

from knowledgeops_agent.core.config import get_settings
from knowledgeops_agent.harness.models.gateway import (
    remove_accounting_budget_limit,
    retire_project_usage,
    upgrade_accounting_budget,
    upgrade_accounting_call_links,
    upgrade_accounting_lifecycle,
)


def main() -> None:
    """保留默认scope；不提供自定义scope参数以免误建一份生产额度。"""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--total-limit", type=int, choices=(1200, 1600, 2000))
    mode.add_argument("--remove-total-limit", action="store_true")
    mode.add_argument("--retire-project-usage", action="store_true")
    mode.add_argument("--lifecycle", action="store_true", help="增加清理封闭标记；不删除旧回执或重置额度")
    args = parser.parse_args()
    settings = get_settings()
    engine = create_engine(settings.accounting_database_url)
    try:
        if args.lifecycle:
            print(json.dumps(upgrade_accounting_lifecycle(engine), ensure_ascii=False))
            return
        if args.retire_project_usage:
            print(json.dumps(retire_project_usage(engine), ensure_ascii=False))
            return
        print(
            json.dumps(
                {
                    "budget": remove_accounting_budget_limit(engine)
                    if args.remove_total_limit
                    else upgrade_accounting_budget(engine, target_limit=args.total_limit),
                    "call_links": upgrade_accounting_call_links(engine),
                },
                ensure_ascii=False,
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    from knowledgeops_agent.persistence.maintenance import managed_main

    managed_main(main)
