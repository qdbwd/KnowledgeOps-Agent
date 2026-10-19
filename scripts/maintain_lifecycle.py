"""生命周期维护入口：默认只预览；--execute 才纳管、接收或推进清理。"""

import argparse
import json

from sqlalchemy import select

from knowledgeops_agent.app.services import create_services
from knowledgeops_agent.core.config import get_settings
from knowledgeops_agent.persistence.models import CleanupJob, MaintenanceState, Run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--run-id", help="预览一个根任务；配合--execute接收其到期清理")
    target.add_argument("--job-id", help="查看已有清理；配合--execute推进")
    target.add_argument("--scan", action="store_true", help="查看/推进每日纳管扫描")
    target.add_argument("--configuration-history", choices=("subagent", "tool_policy", "tool_group"),
                        help="预览一批旧配置修订；--execute才回收无依赖的到期历史")
    parser.add_argument("--after", help="配置历史的分页游标，使用上次next_cursor的JSON数组")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--retry", action="store_true", help="显式重试失败作业，要求--job-id与--execute")
    parser.add_argument("--steps", type=int, default=1, help="最多推进步数，默认1，上限1000")
    args = parser.parse_args()
    if not 1 <= args.steps <= 1000:
        parser.error("--steps must be between 1 and 1000")
    if args.retry and not (args.execute and args.job_id):
        parser.error("--retry requires --execute and --job-id")
    if args.execute and not (args.run_id or args.job_id or args.scan or args.configuration_history):
        parser.error("--execute requires an explicit cleanup target")
    if args.after and not args.configuration_history:
        parser.error("--after requires --configuration-history")
    try:
        config_after = json.loads(args.after) if args.after else None
    except ValueError:
        parser.error("--after must be a JSON array")
    services = create_services(get_settings().model_copy(update={"model_calls_enabled": False}))
    try:
        maintenance = services.lifecycle
        if args.configuration_history:
            result = maintenance.configuration.preview(args.configuration_history, after=config_after,
                                                       limit=services.settings.lifecycle_batch_size)
        elif args.run_id:
            result = maintenance.details.preview(args.run_id)
        elif args.job_id:
            result = maintenance.jobs.get(args.job_id)
        elif args.scan:
            from knowledgeops_agent.app.lifecycle.maintenance import SCAN_STATE
            with services.sessions() as session:
                row = session.get(MaintenanceState, SCAN_STATE)
                result = {"cursor": row.cursor, "next_scan_at": row.next_scan_at} if row else {"status": "not_adopted"}
        else:
            with services.sessions() as session:
                roots = list(session.scalars(select(Run.id).where(Run.id == Run.root_id, Run.lifecycle_state == "active")
                                             .order_by(Run.id).limit(services.settings.lifecycle_batch_size)))
                jobs = list(session.scalars(select(CleanupJob.id).where(CleanupJob.status != "completed")
                                            .order_by(CleanupJob.next_attempt_at).limit(services.settings.lifecycle_batch_size)))
            result = {"mode": "preview", "runs": [maintenance.details.preview(identity) for identity in roots], "pending_jobs": jobs}
        if args.execute:
            identity = args.job_id
            if args.run_id:
                identity = maintenance.details.admit(args.run_id)["id"]
            elif args.retry:
                if result["target_kind"] == "conversation":
                    maintenance.conversations.retry(identity)
                else:
                    maintenance.jobs.retry(identity)
            for _ in range(args.steps):
                if args.configuration_history:
                    result = maintenance.configuration.step(args.configuration_history, after=config_after,
                                                            limit=services.settings.lifecycle_batch_size)
                    config_after = result["next_cursor"]
                    if config_after is None:
                        break
                else:
                    result = maintenance.scan_step() if args.scan else maintenance.advance(identity)
                if result is None or result.get("status") in {"failed", "completed"} or result.get("error"):
                    break
            if identity:
                result = maintenance.jobs.get(identity)
        if isinstance(result, dict):
            result = {key: value for key, value in result.items() if key not in {"claim_token", "claim_until"}}
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    finally:
        services.close()


if __name__ == "__main__":
    main()
