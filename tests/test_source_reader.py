"""全文阅读与材料移除共享不可变版本，不能破坏已经给出的引用。"""

import pytest

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
    result = sources.remove_source(source['id'])
    assert result['deleted'] is True
    assert sources.list_sources() == []
    with pytest.raises(KeyError):
        sources.get_source(source['id'])
    assert sources.get_evidence(evidence['id'])['text'] == '这是历史引用的完整原文'
    assert sources.full_content(source['version_id'])['text'] == '这是历史引用的完整原文'
    assert sources.get_source(source['id'], version_id=source['version_id'])['deleted']
    assert sources.remove_source(source['id']) == result
    # 明确重新导入创建新身份，旧链接继续指向原版本。
    fresh = sources.import_text('再次导入', '这是历史引用的完整原文')
    assert fresh['id'] != source['id']
    assert len(sources.list_sources()) == 1
