"""在新构建镜像和空的临时扩展挂载上验证部署初始化，绝不使用正式业务库。"""

import argparse
import json
import subprocess
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.core.resources import bundled_resource, managed_resource_root
from knowledgeops_agent.harness.skills.bootstrap import initialize_skills
from knowledgeops_agent.harness.skills.management import SkillManagement
from knowledgeops_agent.persistence.models import SkillPackageState


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    settings = Settings()
    if settings.model_calls_enabled or args.output.exists():
        raise ValueError('需要关闭模型并使用新的验证报告')
    root, mcp = managed_resource_root('skills'), managed_resource_root('mcp')
    if any(root.iterdir()) or any(mcp.iterdir()):
        raise ValueError('只能使用专门为空的新挂载，不能验证现有用户目录')
    # 先改库名再创建连接，杜绝把部署环境里的正常DATABASE_URL用于验证写入。
    from sqlalchemy.engine import make_url

    url = make_url(settings.database_url).set(database='knowledgeops_test')
    schema = 'extension_image_' + uuid4().hex
    admin = create_engine(url)
    engine = create_engine(url, connect_args={'options': '-csearch_path=' + schema})
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        with engine.begin() as connection:
            config = Config('/workspace/alembic.ini')
            config.attributes['connection'] = connection
            command.upgrade(config, '0042')
        sessions = sessionmaker(engine, expire_on_commit=False)
        result = initialize_skills(sessions, root, bundled_resource('skill-seeds'))
        assert result['mode'] == 'seed' and len(result['changed']) == 12
        with sessions() as session:
            rows = list(session.scalars(select(SkillPackageState).order_by(SkillPackageState.name)))
            names = [row.name for row in rows]
            name, revision = rows[0].name, rows[0].revision
        assert len(names) == 12
        manager = SkillManagement(sessions, root)
        manager.remove(name, expected_revision=revision)
        second = initialize_skills(sessions, root, bundled_resource('skill-seeds'))
        assert second['changed'] == [] and not (root / name).exists()
        probe = mcp / '.write-check'
        probe.write_text('isolated deployment test')
        probe.unlink()
        report = {
            'schema': '0042', 'isolated_database': url.database, 'isolated_schema': schema,
            'first_initialization': result, 'managed_packages': names,
            'removed_seed_not_restored': True, 'mcp_mount_writable': True,
            'node': subprocess.check_output(['node', '--version'], text=True).strip(),
            'uv': subprocess.check_output(['uv', '--version'], text=True).strip(),
            'model_calls': 0,
        }
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'initialized': 12, 'removed_seed_not_restored': True, 'database': url.database}))
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


if __name__ == '__main__':
    main()
