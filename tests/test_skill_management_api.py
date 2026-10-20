"""真实HTTP/PG文件流程，不使用模型或执行上传脚本。"""

import io
import zipfile

import pytest
from test_api_worker import client as _client
from test_api_worker import services as _services

client, services = _client, _services


@pytest.fixture(autouse=True)
def skill_directory(tmp_path, monkeypatch):
    root = tmp_path / "managed-skills"
    root.mkdir()
    monkeypatch.setenv("SKILLS_DIRECTORY", str(root))
    return root


def upload(client, *, name="example", body="Initial instructions", revision=0, archive=False):
    files = [("SKILL.md", f"---\nname: {name}\ndescription: Example workflow\n---\n{body}".encode()),
             ("scripts/main.py", b"raise RuntimeError('do not execute during import')")]
    if archive:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            for path, data in files:
                bundle.writestr(f"{name}/{path}", data)
        payload = [("files", ("example.zip", buffer.getvalue(), "application/zip"))]
    else:
        payload = [("files", (f"{name}/{path}", data, "application/octet-stream")) for path, data in files]
    return client.post("/api/skills/imports", files=payload, data={"expected_revision": str(revision)})


def test_full_http_package_flow(client, skill_directory, services):
    staged = upload(client, archive=True)
    assert staged.status_code == 201, staged.text
    identity = staged.json()["id"]
    assert client.get("/api/skills").json() == []
    trial = client.post(f"/api/skills/imports/{identity}/try-read", json={"path": "SKILL.md"})
    assert trial.status_code == 200, trial.text
    assert "Initial instructions" in trial.json()["content"]
    assert trial.json()["execution_tested"] is False
    installed = client.post(f"/api/skills/imports/{identity}/commit")
    assert installed.status_code == 200, installed.text
    assert installed.json()["revision"] == 1
    catalog = client.get("/api/skills").json()
    assert len(catalog) == 1 and not catalog[0]["enabled"] and catalog[0]["revision"] == 1
    policy = client.get("/api/tools/policy").json()
    enabled = client.patch("/api/tools/policy", json={"expected_revision": policy["revision"], "skill_enabled": {"example": True}})
    assert enabled.status_code == 200, enabled.text
    assert client.get("/api/skills/example").json()["enabled"]
    downloaded = client.get("/api/skills/example/export")
    assert downloaded.status_code == 200
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as bundle:
        assert bundle.read("example/scripts/main.py").startswith(b"raise RuntimeError")
    update = upload(client, revision=1, body="Updated instructions").json()
    assert client.post(f"/api/skills/imports/{update['id']}/commit").json()["revision"] == 2
    assert "Updated instructions" in client.get("/api/skills/example").json()["content"]
    assert client.get("/api/skills/example").json()["enabled"]
    assert client.request("DELETE", "/api/skills/example", json={"expected_revision": 1}).status_code == 409
    removed = client.request("DELETE", "/api/skills/example", json={"expected_revision": 2})
    assert removed.status_code == 200, removed.text
    assert client.get("/api/skills").json() == []
    assert client.get("/api/skills/example").status_code == 404
    assert not (skill_directory / "example").exists()
    restored = upload(client).json()
    assert client.post(f"/api/skills/imports/{restored['id']}/commit").json()["revision"] == 4
    assert not client.get("/api/skills/example").json()["enabled"]
    assert services.gateway.ledger.records() == []


def test_metadata_completion_invalid_paths_and_cancelled_preview(client, skill_directory):
    draft = client.post("/api/skills/imports", files={"files": ("SKILL.md", b"Plain markdown")})
    assert draft.status_code == 201, draft.text
    assert draft.json()["preview"]["missing_metadata"] == ["name", "description"]
    assert client.post(f"/api/skills/imports/{draft.json()['id']}/commit").status_code == 409
    ready = client.post("/api/skills/imports", files={"files": ("SKILL.md", b"Plain markdown")},
                        data={"name": "reader", "description": "Read documents"})
    assert ready.status_code == 201, ready.text
    identity = ready.json()["id"]
    assert client.get(f"/api/skills/imports/{identity}").json()["preview"]["missing_metadata"] == []
    assert client.post(f"/api/skills/imports/{identity}/try-read", json={"path": "../secrets"}).status_code == 404
    assert client.delete(f"/api/skills/imports/{identity}").status_code == 200
    assert client.post(f"/api/skills/imports/{identity}/commit").status_code == 409
    bad = client.post("/api/skills/imports", files=[("files", ("SKILL.md", b"instructions")), ("files", ("../secret", b"bad"))])
    assert bad.status_code == 422, bad.text
    assert not (skill_directory.parent / "secret").exists()


def test_unsupported_behavior_can_be_reviewed_but_not_enabled(client):
    staged = client.post("/api/skills/imports", files={"files": ("SKILL.md", b"---\nname: example\ndescription: example\nhooks: {}\n---\ninstructions")})
    assert staged.status_code == 201, staged.text
    assert not staged.json()["preview"]["compatible"]
    assert client.post(f"/api/skills/imports/{staged.json()['id']}/commit").status_code == 200
    item = client.get("/api/skills").json()[0]
    assert not item["available"]
    response = client.patch("/api/tools/policy", json={"expected_revision": item["policy_revision"], "skill_enabled": {"example": True}})
    assert response.status_code == 409
