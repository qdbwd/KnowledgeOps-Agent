"""容器内显式重建PG历史片段索引，不修改原话、批次、检查点或模型账本。"""

import json

from knowledgeops_agent.core.config import get_settings
from knowledgeops_agent.persistence.models import make_sessions
from knowledgeops_agent.retrieval.index import rebuild_history_index
from knowledgeops_agent.retrieval.search import INDEX_CONTRACT


def main():
    settings = get_settings()
    sessions = make_sessions(settings.database_url, workspace_root=settings.workspace_root)
    try:
        print(
            json.dumps(
                {
                    **rebuild_history_index(sessions),
                    "index_contract": INDEX_CONTRACT,
                    "model_calls": 0,
                }
            )
        )
    finally:
        sessions.kw["bind"].dispose()


if __name__ == "__main__":
    main()
