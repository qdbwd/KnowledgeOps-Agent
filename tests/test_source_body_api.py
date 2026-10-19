"""网页正文入口替代HTML下载，上传原件及固定正文继续可读。"""

from test_api_worker import client as client
from test_api_worker import services as services


def test_web_original_endpoint_directs_to_saved_body(client, services):
    source = services.sources.import_text(
        "网页", "# 正文\n\n完整保存的内容。", kind="web", origin="https://example.org"
    )
    response = client.get(f"/api/source-versions/{source['version_id']}/original")
    assert response.status_code == 410
    body = client.get(response.json()["detail"]["content_url"])
    assert body.status_code == 200 and body.json()["text"] == "# 正文\n\n完整保存的内容。"
    assert body.json()["integrity"] == "verified"
    file = client.post(
        "/api/sources/upload", files={"file": ("文档.md", "# 本地文件".encode(), "text/markdown")}
    ).json()
    assert client.get(f"/api/source-versions/{file['version_id']}/original").text == "# 本地文件"


def test_delivered_file_api_reads_snapshot_after_workfile_is_removed(client, services, tmp_path):
    from knowledgeops_agent.sources.deliveries import DeliveryService

    services.settings.workspace_root = tmp_path
    (tmp_path / "outputs").mkdir()
    file = tmp_path / "outputs/结果.md"
    file.write_text("# 固定结果", encoding="utf-8")
    delivery = DeliveryService(services.sessions, tmp_path).present(
        ["outputs/结果.md"],
        run_id="run",
        root_id="root",
        call_id="call",
    )[0]
    file.unlink()
    assert client.get("/api/deliveries").json()[0]["id"] == delivery["id"]
    assert client.get(delivery["url"]).text == "# 固定结果"
    assert client.get(f"/api/deliveries/{delivery['id']}/preview").json()["text"] == "# 固定结果"
    assert client.get(delivery["url"]).headers["content-disposition"].startswith("attachment;")


def test_conversation_upload_and_save_share_one_fixed_version(client, services):
    conversation = services.jobs.create_conversation("本次资料")
    upload = client.post(f"/api/sources/upload?conversation_id={conversation['id']}",
                         files={"file": ("附件.md", "# 会话附件".encode(), "text/markdown")}).json()
    assert upload["independently_saved"] is False
    assert client.get("/api/sources").json() == []
    scoped = client.get(f"/api/sources?conversation_id={conversation['id']}&include_library=false").json()
    assert scoped[0]["id"] == upload["id"]
    saved = client.post(f"/api/sources/{upload['id']}/save").json()
    assert saved["independently_saved"] is True and saved["version_id"] == upload["version_id"]
    assert client.get("/api/sources").json()[0]["id"] == upload["id"]


def test_asset_removal_requires_current_preview_and_exposes_retryable_progress(client, services):
    source = services.sources.import_text("删除接口", "无依赖正文")
    for _ in range(100):
        if services.lifecycle.retained.catalog.step(limit=100)["complete"]:
            break
    path = f"/api/assets/source/{source['id']}"
    preview = client.get(path + "/deletion-preview").json()
    assert preview["eligible"] and preview["counts"]["delete"] == 1
    assert client.delete(f"/api/sources/{source['id']}").status_code == 422
    assert client.post(path + "/deletion", json={"expected_version": "0" * 64}).status_code == 409
    response = client.post(path + "/deletion", json={"expected_version": preview["version"]})
    assert response.status_code == 202
    job = response.json()
    assert not {"cursor", "claim_token", "claim_until"}.intersection(job)
    assert client.get("/api/asset-deletions").json()[0]["id"] == job["id"]
    for _ in range(10):
        services.lifecycle.advance(job["id"])
        if services.lifecycle.assets.get_job(job["id"])["status"] == "completed":
            break
    result = client.get(f"/api/asset-deletions/{job['id']}").json()
    assert result["status"] == "completed" and result["summary"]["deleted"] == 1
    assert client.get(f"/api/source-versions/{source['version_id']}/content").status_code == 404
    assert client.post(path + "/deletion", json={"expected_version": preview["version"]}).json()["id"] == job["id"]
