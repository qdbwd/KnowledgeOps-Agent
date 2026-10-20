"""由backup.ps1调用的只读扩展打包入口；不安装、不连接任何MCP服务。"""

import argparse
import json
import re
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.core.resources import managed_resource_root, resource_path
from knowledgeops_agent.persistence.extensions import create_extensions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fixture-root', type=Path)
    parser.add_argument('--include-data', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'knowledgeops(?:_backupfixture_[a-f0-9]{12})?', args.database):
        parser.error('只允许本应用或明确的备份演练数据库')
    if args.fixture_root and (args.database == 'knowledgeops' or not re.fullmatch(
            r'/workspace/artifacts/verification/backup-[a-f0-9]{12}/extensions', str(args.fixture_root))):
        parser.error('自定义目录仅用于隔离演练')
    if args.database != 'knowledgeops' and not args.fixture_root:
        parser.error('演练必须显式提供自己的扩展目录')
    skills = args.fixture_root / 'skills' if args.fixture_root else resource_path('SKILLS_DIRECTORY', managed_resource_root('skills'))
    mcp = args.fixture_root / 'mcp' if args.fixture_root else resource_path('MCP_DIRECTORY', managed_resource_root('mcp'))
    engine = create_engine(make_url(Settings(model_calls_enabled=False).database_url).set(database=args.database))
    try:
        print(json.dumps(create_extensions(sessionmaker(engine), skills=skills, mcp=mcp, output=args.output,
                                          include_data=args.include_data)))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
