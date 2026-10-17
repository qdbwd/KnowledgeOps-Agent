"""在真实临时文件系统验证精确受管地址、哈希与所有层级符号链接边界。"""

import hashlib
import os
from uuid import uuid4

import pytest

from knowledgeops_agent.app.lifecycle.files import remove_artifact_file, remove_operation_file
from knowledgeops_agent.persistence.files import FileStore


def test_internal_file_removal_is_idempotent_and_never_removes_work_outputs(tmp_path):
    store, run = FileStore(tmp_path), str(uuid4())
    digest = hashlib.sha256(b"internal").hexdigest()
    key = f"artifacts/{run}/{digest}.txt"
    store.put(key, "internal")
    store.put("outputs/user.md", "keep")
    assert remove_artifact_file(store, run_id=run, content_hash=digest, storage_key=key)
    assert not remove_artifact_file(store, run_id=run, content_hash=digest, storage_key=key)
    with pytest.raises(ValueError, match="managed identity"):
        remove_artifact_file(store, run_id=run, content_hash=digest, storage_key="outputs/user.md")
    assert store.path("outputs/user.md").read_text() == "keep"


def test_operation_tree_manifest_refuses_changed_content_and_new_leaf_blocks_completion(tmp_path):
    from knowledgeops_agent.app.lifecycle.files import (
        operation_tree_manifest,
        remove_operation_tree_entry,
    )
    from knowledgeops_agent.harness.sandbox.files import file_hash
    store, operation = FileStore(tmp_path), str(uuid4())
    key = f"artifacts/file-operations/{operation}/deleted"
    store.replace(key + "/old.md", b"old")
    digest = file_hash(store.path(key))
    store.replace(key + "/old.md", b"changed")
    with pytest.raises(ValueError, match="tree changed"):
        operation_tree_manifest(store, operation, digest)
    store.replace(key + "/old.md", b"old")
    manifest = operation_tree_manifest(store, operation, digest)
    store.replace(key + "/new.md", b"new")
    assert remove_operation_tree_entry(store, operation, manifest[0])
    with pytest.raises(OSError):
        remove_operation_tree_entry(store, operation, manifest[1])
    assert store.path(key + "/new.md").read_bytes() == b"new"


@pytest.mark.parametrize("link_parent", [False, True])
def test_symlinks_are_rejected_even_when_they_point_inside_workspace(tmp_path, link_parent):
    store, run = FileStore(tmp_path), str(uuid4())
    digest = hashlib.sha256(b"keep").hexdigest()
    key = f"artifacts/{run}/{digest}.txt"
    target = store.path("outputs/keep.txt")
    store.put("outputs/keep.txt", "keep")
    if link_parent:
        (tmp_path / "artifacts").mkdir()
        (tmp_path / "artifacts" / run).symlink_to(target.parent, target_is_directory=True)
        store.put("outputs/" + digest + ".txt", "keep")
    else:
        path = tmp_path / key
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
    with pytest.raises(OSError):
        remove_artifact_file(store, run_id=run, content_hash=digest, storage_key=key)
    assert target.read_text() == "keep"


def test_changed_receipt_is_protected_and_exact_deleted_copy_can_be_removed(tmp_path):
    store, operation = FileStore(tmp_path), str(uuid4())
    key = f"artifacts/file-operations/{operation}/deleted"
    digest = store.put(key, "original")
    store.path(key).write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        remove_operation_file(store, operation_id=operation, digest=digest, deleted=True)
    assert store.path(key).read_text() == "changed"
    store.path(key).write_text("original")
    assert remove_operation_file(store, operation_id=operation, digest=digest, deleted=True)


def test_path_injection_in_owner_is_not_accepted(tmp_path):
    store = FileStore(tmp_path)
    with pytest.raises(ValueError):
        remove_operation_file(store, operation_id="../../outputs", digest="a" * 64)


def test_non_regular_internal_path_is_not_opened_as_a_blocking_stream(tmp_path):
    store, operation = FileStore(tmp_path), str(uuid4())
    path = store.path(f"artifacts/file-operations/{operation}/content")
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular files"):
        remove_operation_file(store, operation_id=operation, digest="a" * 64)
    assert path.exists()
