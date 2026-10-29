"""APP-05 备份记录只读投影：目录存在不等于成功，校验失败要如实标出。"""

import json

from knowledgeops_agent.app.backup_records import BackupRecords


def write(root, name, relative, payload):
    path = root / name / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_records_report_verified_created_and_incomplete(tmp_path):
    write(tmp_path, "b1", "manifest.json", {"format": 3, "created_at": "2026-10-24T00:00:00Z"})
    write(tmp_path, "b1", "operations/op1.json",
          {"id": "op1", "kind": "backup", "state": "complete", "phase": "complete"})
    write(tmp_path, "b1", "restore-verification.json",
          {"database": "restore_b1", "checks": {"hashes": True, "references": True}})
    write(tmp_path, "b2", "manifest.json", {"format": 2, "created_at": "2026-10-23T00:00:00Z"})
    write(tmp_path, "b3", "operations/op3.json", {"id": "op3", "kind": "backup", "state": "failed"})
    write(tmp_path, "b4", "manifest.json", {"format": 3})
    write(tmp_path, "b4", "restore-verification.json", {"checks": {"hashes": True, "bodies": False}})

    cards = BackupRecords(tmp_path).list()
    assert cards["available"] is True
    by_id = {item["id"]: item for item in cards["items"]}
    assert by_id["b1"]["status"] == "verified" and by_id["b1"]["restore"]["passed"] is True
    assert by_id["b1"]["operations"][0]["kind"] == "backup"
    assert by_id["b2"]["status"] == "created" and by_id["b2"]["restore"]["checked"] is False
    assert by_id["b3"]["status"] == "incomplete"
    assert by_id["b4"]["status"] == "created"
    assert by_id["b4"]["restore"]["failed_checks"] == ["bodies"]


def test_missing_root_is_reported_instead_of_invented(tmp_path):
    cards = BackupRecords(tmp_path / "absent").list()
    assert cards["available"] is False and cards["items"] == []
