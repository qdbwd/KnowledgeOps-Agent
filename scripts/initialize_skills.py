"""迁移后接管已有技能，或在全新安装中复制一次初始模板。"""

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.core.resources import bundled_resource, managed_resource_root, resource_path
from knowledgeops_agent.harness.skills.bootstrap import initialize_skills


def main():
    settings = Settings(model_calls_enabled=False)
    engine = create_engine(settings.database_url)
    try:
        result = initialize_skills(
            sessionmaker(engine, expire_on_commit=False),
            resource_path("SKILLS_DIRECTORY", managed_resource_root("skills")),
            bundled_resource("skill-seeds"),
        )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
