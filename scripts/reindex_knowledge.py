"""容器内显式重建可再生词法索引；不改正文、历史、引用或模型账本。"""

import json

from knowledgeops_agent.core.config import get_settings
from knowledgeops_agent.knowledge.service import KnowledgeService
from knowledgeops_agent.persistence.models import make_sessions
from knowledgeops_agent.retrieval.search import INDEX_CONTRACT


def main():
    settings = get_settings()
    sessions = make_sessions(settings.database_url, workspace_root=settings.workspace_root)
    try:
        print(
            json.dumps(
                {
                    "indexed_documents": KnowledgeService(sessions).rebuild_index(),
                    "index_contract": INDEX_CONTRACT,
                    "model_calls": 0,
                }
            )
        )
    finally:
        sessions.kw["bind"].dispose()


if __name__ == "__main__":
    from knowledgeops_agent.persistence.maintenance import managed_main

    managed_main(main)
