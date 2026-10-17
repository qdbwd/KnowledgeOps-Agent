"""PG 图重开和真实子图 interrupt 的恢复合同；不扩大生产子角色工具权限。"""

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Command, interrupt
from psycopg import Connection
from psycopg.rows import dict_row
from test_execution_harness import Commands, save_wait
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run
from test_workspace_runtime import enable

from knowledgeops_agent.app.runs.children import ChildRuns
from knowledgeops_agent.app.services import Services
from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.harness.permissions.approvals import ExecutionApprovals
from knowledgeops_agent.harness.permissions.receipts import CommandJournal
from knowledgeops_agent.harness.runtime.resources import prepare_resources
from knowledgeops_agent.harness.subagents.delegation import research_many

runtime_services = _runtime_services


def test_pg_checkpoint_reopen_preserves_approval_and_does_not_repeat_side_effect(
    runtime_services, tmp_path
):
    module, context, run = setup_run(runtime_services)
    context.workspace_root = tmp_path / "workspace"
    enable(context.jobs.sessions)
    context.gateway.client = Commands(["printf 'one' >> work/proof"])
    engine = context.gateway.ledger.engine
    args, kwargs = engine.dialect.create_connect_args(engine.url)
    with engine.connect() as connection:
        schema = connection.exec_driver_sql("SELECT current_schema()").scalar_one()

    def connect():
        return Connection.connect(
            *args,
            **kwargs,
            options=f"-csearch_path={schema}",
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        )

    config = {"configurable": {"thread_id": run["checkpoint_id"]}}
    with connect() as connection:
        saver = PostgresSaver(connection)
        saver.setup()
        graph = module.build_agent(context, checkpointer=saver)
        graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
        approvals, _ = save_wait(context, run, graph, config)
    context.jobs.recover_on_startup()
    row = approvals.list()[0]
    approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=True)
    context.jobs.claim(context.worker_id, run_id=run["id"], generation=2)
    with connect() as connection:
        graph = module.build_agent(context, checkpointer=PostgresSaver(connection))
        state = graph.invoke(
            approvals.resume_input(graph.get_state(config)), config, context=context
        )
        assert state["messages"][-1].text == "处理完毕。"
    assert (context.workspace_root / "work/proof").read_text() == "one"
    receipt = CommandJournal(context).list()[0]
    assert receipt["status"] == "succeeded"
    call = {"name": "execute", "id": "command-0", "args": {"command": "printf 'one' >> work/proof"}}
    assert CommandJournal(context).start(
        call, workspace=str(context.workspace_root), cwd=str(context.workspace_root)
    )["cached"]
    assert context.jobs.get_run(run["id"])["tool_calls"] == 1
    assert (context.workspace_root / "work/proof").read_text() == "one"


def test_two_readonly_child_interrupts_stay_children_and_resume_original_checkpoints(
    runtime_services, monkeypatch, tmp_path
):
    module, context, run = setup_run(runtime_services)
    context.workspace_root = tmp_path / "workspace"
    context.checkpointer = InMemorySaver()
    prepare_resources(context)

    def build_child(child_context, *, checkpointer):
        # 单独构造真实只读子图，验证委派适配器；生产角色仍没有 execute。
        def read_step(state):
            call = {
                "name": "read_source",
                "id": "readonly-review",
                "args": {"segment_id": "fixture"},
            }
            row = ExecutionApprovals(child_context)._ensure(call, state, origin="tool")
            decision = interrupt({"execution_action_id": row["id"]})
            return {
                "messages": [
                    AIMessage(
                        "用户拒绝后，子研究保留进展。"
                        if decision["decision"] == "reject"
                        else "done"
                    )
                ]
            }

        graph = StateGraph(MessagesState)
        graph.add_node("read_step", read_step)
        graph.add_edge(START, "read_step")
        graph.add_edge("read_step", END)
        return graph.compile(checkpointer=checkpointer)

    import knowledgeops_agent.app.runs.worker as worker

    monkeypatch.setattr(worker, "build_agent", build_child)
    services = Services(settings=Settings(_env_file=None, model_calls_enabled=False,
                                         workspace_root=context.workspace_root),
                        engine=context.jobs.sessions.kw["bind"], sessions=context.jobs.sessions,
                        jobs=context.jobs, knowledge=context.knowledge, sources=context.sources,
                        gateway=context.gateway)
    builder = StateGraph(dict)
    builder.add_node("delegate", lambda state: {
        "results": research_many(context, ["读取一", "读取二"], "children")})
    builder.add_edge(START, "delegate")
    builder.add_edge("delegate", END)
    graph = builder.compile(checkpointer=context.checkpointer)
    config = {"configurable": {"thread_id": run["checkpoint_id"]}}
    graph.invoke({}, config)
    children = [r for r in context.jobs.list_runs() if r["parent_id"] == run["id"]]
    lifecycle = ChildRuns(context.jobs)
    lifecycle.wait(run["id"], context.worker_id, [row["id"] for row in children])
    for child in children:
        worker.execute_run(child["id"], child["generation"], services=services,
                           checkpointer=context.checkpointer)
    children = [r for r in context.jobs.list_runs() if r["parent_id"] == run["id"]]
    assert len(children) == 2 and all(r["status"] == "interrupted" for r in children)
    assert context.jobs.get_run(run["id"])["status"] == "waiting_children"
    original_checkpoints = {row["id"]: row["checkpoint_id"] for row in children}
    approvals = ExecutionApprovals(context)
    decisions = approvals.list()
    assert len(decisions) == 2
    for index, row in enumerate(decisions):
        approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=False)
        generation = context.jobs.get_run(row["run_id"])["generation"]
        approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=False)
        assert context.jobs.get_run(row["run_id"])["generation"] == generation
        if index == 0:
            assert context.jobs.get_run(row["run_id"])["status"] == "queued"
            context.jobs.pause(run["id"])
    assert context.jobs.get_run(run["id"])["status"] == "paused"
    assert all(context.jobs.get_run(row["id"])["status"] != "queued" for row in children)
    context.jobs.resume(run["id"])
    for child in children:
        queued = context.jobs.get_run(child["id"])
        worker.execute_run(queued["id"], queued["generation"], services=services,
                           checkpointer=context.checkpointer)
    queued_root = context.jobs.get_run(run["id"])
    assert context.jobs.claim(context.worker_id, run_id=run["id"], generation=queued_root["generation"])
    result = graph.invoke(Command(resume=lifecycle.resume_answers(run["id"], graph.get_state(config))), config)["results"]
    assert {r["run_id"] for r in result} == {r["id"] for r in children}
    assert all(r["status"] == "completed" and "用户拒绝" in r["answer"] for r in result)
    assert {row["id"]: context.jobs.get_run(row["id"])["checkpoint_id"] for row in children} == original_checkpoints
