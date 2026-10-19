"""交付固定内容与恢复幂等，使用真实文件和独立PG，不请求模型。"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from knowledgeops_agent.persistence.models import DeliveredFile
from knowledgeops_agent.sources.deliveries import DeliveryService


def test_delivery_is_independent_of_later_workfile_changes_and_replay(business_sessions, tmp_path):
    (tmp_path / "outputs").mkdir()
    file = tmp_path / "outputs/report.md"
    file.write_text("# 已交付\n第一版", encoding="utf-8")
    service = DeliveryService(business_sessions, tmp_path)
    first = service.present(["/outputs/report.md"], run_id="run", root_id="root", call_id="call")
    file.write_text("# 后续工作\n第二版", encoding="utf-8")
    assert service.preview(first[0]["id"])["text"] == "# 已交付\n第一版"
    assert (
        service.present(["/outputs/report.md"], run_id="run", root_id="root", call_id="call")
        == first
    )
    second = service.present(
        ["/outputs/report.md"], run_id="run", root_id="root", call_id="another"
    )
    assert second[0]["id"] != first[0]["id"]
    file.unlink()
    assert service.read(first[0]["id"])[1] == "# 已交付\n第一版".encode()
    assert len(service.list()) == 2
    with pytest.raises(ValueError, match="different request"):
        service.present(["/outputs/other.md"], run_id="run", root_id="root", call_id="call")


def test_concurrent_replay_commits_one_delivery_and_binary_download_is_exact(
    business_sessions, tmp_path
):
    (tmp_path / "outputs").mkdir()
    content = b"\x00\x01\xff\x10binary"
    (tmp_path / "outputs/data.bin").write_bytes(content)
    service = DeliveryService(business_sessions, tmp_path)

    def present():
        return service.present(["outputs/data.bin"], run_id="run", root_id="root", call_id="once")

    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = list(pool.map(lambda _: present(), range(2)))
    assert one == two
    assert service.read(one[0]["id"])[1] == content
    assert service.preview(one[0]["id"])["text"] is None
    with business_sessions() as session:
        assert session.scalar(select(func.count()).select_from(DeliveredFile)) == 1


def test_delivery_validates_all_targets_and_detects_saved_file_corruption(
    business_sessions, tmp_path
):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs/good.txt").write_text("固定内容", encoding="utf-8")
    service = DeliveryService(business_sessions, tmp_path)
    with pytest.raises(KeyError):
        service.present(
            ["outputs/good.txt", "outputs/missing.txt"], run_id="run", root_id="root", call_id="bad"
        )
    assert service.list() == []
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_text("outside")
    (tmp_path / "outputs/escape.txt").symlink_to(outside)
    with pytest.raises(ValueError):
        service.present(["outputs/escape.txt"], run_id="run", root_id="root", call_id="escape")
    first = service.present(["outputs/good.txt"], run_id="run", root_id="root", call_id="good")
    with business_sessions() as session:
        row = session.get(DeliveredFile, first[0]["id"])
        (tmp_path / row.storage_key).write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        service.read(first[0]["id"])


def test_cancelled_delivery_cannot_be_registered(business_sessions, tmp_path):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs/a.txt").write_text("内容")
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise PermissionError("cancelled")

    service = DeliveryService(business_sessions, tmp_path)
    with pytest.raises(PermissionError, match="cancelled"):
        service.present(
            ["outputs/a.txt"], run_id="run", root_id="root", call_id="cancelled", guard=guard
        )
    assert service.list() == []
