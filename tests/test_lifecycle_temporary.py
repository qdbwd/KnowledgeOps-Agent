"""真实文件锁和文件系统验证：只清理登记的死暂存，不按后缀猜测用户文件。"""

from datetime import timedelta

from knowledgeops_agent.persistence.models import new_id, utcnow
from knowledgeops_agent.persistence.temporary import ManagedTemporary, collect_temporaries


def test_old_file_is_protected_while_writer_holds_lease_then_reclaimed(tmp_path):
    item = ManagedTemporary(tmp_path, kind="atomic")
    item.stream.write(b"partial data")
    item.stream.flush()
    future = utcnow() + timedelta(days=2)
    assert collect_temporaries(tmp_path, now=future)["removed"] == 0
    assert item.path.exists()
    item.close(remove=False)
    result = collect_temporaries(tmp_path, now=future)
    assert result["removed"] == 1 and not item.path.exists()
    assert collect_temporaries(tmp_path, now=future)["removed"] == 0


def test_grace_and_owner_recovery_keep_command_capture(tmp_path):
    owner = new_id()
    item = ManagedTemporary(tmp_path, kind="command-output", owner_id=owner)
    item.close(remove=False)
    assert collect_temporaries(tmp_path, now=utcnow(), owner_released=lambda _: True)["removed"] == 0
    future = utcnow() + timedelta(days=2)
    assert collect_temporaries(tmp_path, now=future, owner_released=lambda _: False)["removed"] == 0
    assert collect_temporaries(tmp_path, now=future, owner_released=lambda value: value == owner)["removed"] == 1


def test_unknown_files_and_symlinks_survive_and_batches_resume(tmp_path):
    user = tmp_path / "my.tmp"
    user.write_text("user asset")
    first = ManagedTemporary(tmp_path, kind="upload")
    first.close(remove=False)
    linked = ManagedTemporary(tmp_path, kind="atomic")
    linked.close(remove=False)
    linked.path.unlink()
    linked.path.symlink_to(user)
    unknown = first.path.parent / "not-ours.tmp"
    unknown.write_text("not registered")
    result = collect_temporaries(tmp_path, now=utcnow() + timedelta(days=2), limit=1)
    total = result["removed"]
    while result["next_cursor"]:
        result = collect_temporaries(tmp_path, now=utcnow() + timedelta(days=2), after=result["next_cursor"], limit=1)
        total += result["removed"]
    assert total == 1
    assert user.read_text() == "user asset" and unknown.exists() and linked.path.is_symlink()
