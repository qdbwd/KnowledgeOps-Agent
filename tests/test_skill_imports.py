"""完整包导入只处理文件，不运行包代码；预览和最终规范化用同一合同。"""

import io
import stat
import zipfile

import pytest

from knowledgeops_agent.harness.skills.imports import inspect_package, unpack_zip
from knowledgeops_agent.harness.skills.packages import SkillPackageError


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for path, content in files:
            bundle.writestr(path, content)
    return output.getvalue()


def test_plain_markdown_can_be_completed_without_changing_its_body():
    files = [("skill.md", "# 使用方法\n\n先阅读材料。\n".encode())]
    draft = inspect_package(files)
    assert draft.missing == ["name", "description"]
    ready = inspect_package(files, name="reader", description="读取材料")
    assert ready.missing == []
    assert ready.files["SKILL.md"].decode().endswith("# 使用方法\n\n先阅读材料。\n")
    assert "name: reader" in ready.files["SKILL.md"].decode()
    assert ready.compatible


def test_folder_and_zip_preserve_scripts_binary_resources_and_nested_examples():
    content = b"---\nname: example\ndescription: example\n---\nRun scripts/main.py\n"
    files = [("example/SKILL.md", content), ("example/scripts/main.py", b"raise RuntimeError('never import')"),
             ("example/assets/input.bin", b"\xff\x00"), ("example/tests/sample/SKILL.md", b"example fixture")]
    direct = inspect_package(files)
    zipped = inspect_package(unpack_zip(archive(files)))
    assert direct.files == zipped.files
    assert direct.digest == zipped.digest
    assert direct.files["assets/input.bin"] == b"\xff\x00"
    assert len(direct.files) == 4
    assert direct.name == "example"


@pytest.mark.parametrize("path", ["../escape", "/etc/passwd", "C:/escape", "x\\file", "x/../file", "x/file:stream", "x/NUL", "x/.env"])
def test_unsafe_paths_are_rejected_before_writing(path):
    with pytest.raises(SkillPackageError):
        unpack_zip(archive([("SKILL.md", b"instructions"), (path, b"data")]))


def test_archive_links_duplicates_and_case_collisions_are_rejected():
    target = zipfile.ZipInfo("link")
    target.create_system = 3
    target.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(SkillPackageError, match="链接|普通文件"):
        unpack_zip(archive([("SKILL.md", b"instructions"), (target, b"/private")]))
    with pytest.raises(SkillPackageError, match="重复"):
        inspect_package([("SKILL.md", b"instructions"), ("file.txt", b"1"), ("FILE.txt", b"2")])


def test_two_roots_are_not_silently_combined():
    with pytest.raises(SkillPackageError, match="一个|同一"):
        inspect_package([("one/SKILL.md", b"one"), ("two/SKILL.md", b"two")])


@pytest.mark.parametrize("files", [
    [("scripts", b"file"), ("scripts/main.py", b"script")],
    [("scripts/main.py", b"script"), ("SCRIPTS", b"file")],
])
def test_file_and_directory_collisions_fail_before_staging(files):
    with pytest.raises(SkillPackageError, match="文件与目录"):
        inspect_package([("SKILL.md", b"instructions"), *files])


def test_compatibility_declarations_are_reported_separately_from_format():
    data = b"---\nname: example\ndescription: example\nallowed-tools: [execute]\n---\nworkflow"
    result = inspect_package([("SKILL.md", data)])
    assert not result.compatible
    assert "allowed-tools" in str(result.issues)
    assert result.missing == []
    assert result.files["SKILL.md"] == data


def test_malformed_yaml_and_oversized_packages_are_rejected(monkeypatch):
    with pytest.raises(SkillPackageError, match="YAML"):
        inspect_package([("SKILL.md", b"---\nname: [\n---\nbody")])
    monkeypatch.setattr("knowledgeops_agent.harness.skills.imports.MAX_FILE_BYTES", 10)
    with pytest.raises(SkillPackageError, match="大小"):
        unpack_zip(archive([("SKILL.md", b"x" * 11)]))
