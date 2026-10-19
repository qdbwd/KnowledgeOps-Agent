"""分类详情按既有期限回收，记忆及类别不因清理后台任务而消失。"""

from datetime import timedelta

import pytest
from test_api_worker import services as _services
from test_lifecycle_details import checkpoint_case as _checkpoint_case
from test_lifecycle_details import detail_case as _detail_case
from test_lifecycle_details import ledger as _ledger
from test_lifecycle_details import redis_case as _redis_case
from test_memory_classification import Classifier, consume

from knowledgeops_agent.app.lifecycle.maintenance import LifecycleMaintenance
from knowledgeops_agent.memory.categories import MemoryCategories
from knowledgeops_agent.memory.classification import queue_classification
from knowledgeops_agent.memory.service import MemoryService
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.models import Run

services, detail_case, ledger = _services, _detail_case, _ledger
checkpoint_case, redis_case = _checkpoint_case, _redis_case


def test_expired_classification_details_are_reclaimed_without_removing_user_memory(
    services, detail_case, ledger, tmp_path
):
    details, jobs, _, _, clock, _, _, _, _ = detail_case
    details.sessions.configure(info={"file_store": FileStore(tmp_path)})
    services.jobs = jobs
    services.gateway.ledger = ledger
    services.gateway.enabled, services.gateway.client = True, Classifier()
    memory_service = MemoryService(
        details.sessions,
        on_created=lambda session, row: queue_classification(
            session, jobs, services.gateway, row, "retained-classification"
        ),
    )
    original = memory_service.create(
        "我是 Python 开发者", scope="global", topic="", display_category_id=None
    )
    identity = original["classification"]["run_id"]
    assert consume(services, identity)["status"] == "completed"
    classified = memory_service.get(original["id"])
    history = memory_service.history(original["id"])
    details.eligibility.refresh_retention()
    clock[0] += timedelta(days=31)
    maintenance = LifecycleMaintenance(details, enabled=False, run_service=jobs)
    for _ in range(30):
        if maintenance.retained.catalog.step(limit=100)["complete"]:
            break
    job = details.admit(identity)
    for _ in range(150):
        state = maintenance.jobs.get(job["id"])
        assert state["status"] != "failed", state
        if state["status"] == "completed":
            break
        maintenance.advance(job["id"])
    else:
        pytest.fail("memory classification cleanup did not finish")
    assert memory_service.get(original["id"]) == classified
    assert memory_service.history(original["id"]) == history
    assert classified["display_category_id"] in {
        row["id"] for row in MemoryCategories(details.sessions).list()
    }
    assert ledger.records(root_id=identity) == []
    with details.sessions() as session:
        run = session.get(Run, identity)
        assert run.lifecycle_state == "expired"
        assert "content" not in run.input_data and "categories" not in run.input_data
    with pytest.raises(ValueError, match="清理"):
        jobs.resume(identity)
