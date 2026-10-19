"""全文阅读与材料移除共享不可变版本，不能破坏已经给出的引用。"""

from knowledgeops_agent.app.lifecycle.asset_cleanup import AssetCleaner
from knowledgeops_agent.app.lifecycle.catalog import ReferenceCatalog
from knowledgeops_agent.app.lifecycle.jobs import CleanupJobs
from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.sources.service import SourceService


def test_full_reader_joins_all_segments_without_creating_evidence(business_sessions, tmp_path):
    sources = SourceService(business_sessions, tmp_path)
    text = '# 标题\n\n' + '完整原文\n' * 1500
    first = sources.import_text('完整.md', text, kind='file')
    sources.import_text('另一个版本', '不是旧版本', kind='web', origin='https://example.org')
    content = sources.full_content(first['version_id'])
    assert content['text'] == text
    assert len(content['segments']) > 1
    assert content['segments'][-1]['end'] == len(text)


def test_remove_source_hides_selection_but_preserves_pinned_references(business_sessions, tmp_path):
    sources = SourceService(business_sessions, tmp_path)
    source = sources.import_text('被引用材料', '这是历史引用的完整原文')
    jobs = RunService(business_sessions)
    chat = jobs.create_conversation()
    run = jobs.enqueue(chat['id'], '读取材料', request_key='source-reader')
    evidence = sources.read_segment(source['segments'][0]['id'], root_id=run['id'])
    catalog = ReferenceCatalog(business_sessions)
    for _ in range(100):
        if catalog.step(limit=100)['complete']:
            break
    cleaner = AssetCleaner(business_sessions, CleanupJobs(business_sessions))
    preview = cleaner.preview('source', source['id'])
    result = sources.remove_source(source['id'], expected_version=preview['version'])
    assert result['status'] == 'pending'
    assert sources.list_sources() == []
    # 另一会话已读取该材料；移出材料库不应移除它的使用关系。
    assert sources.get_source(source['id'])['id'] == source['id']
    assert sources.get_evidence(evidence['id'])['text'] == '这是历史引用的完整原文'
    assert sources.full_content(source['version_id'])['text'] == '这是历史引用的完整原文'
    assert sources.remove_source(source['id'], expected_version=preview['version'])['id'] == result['id']
    assert cleaner.step(identity=result['id'])['status'] == 'completed'
    # 共享身份尚有会话使用时重新保存复用它，旧引用保持原固定版本。
    fresh = sources.import_text('再次导入', '这是历史引用的完整原文')
    assert fresh['id'] == source['id']
    assert len(sources.list_sources()) == 1
