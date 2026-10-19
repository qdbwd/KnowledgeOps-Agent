"""历史HTML维护：默认只读报告，执行需精确版本、已审阅报告和同批恢复证据。"""

import argparse
import json
from pathlib import Path

from knowledgeops_agent.app.lifecycle.legacy_html import LegacyHtmlCleaner
from knowledgeops_agent.core.config import get_settings
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.models import make_sessions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--after")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-report", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--version-id", action="append", default=[])
    parser.add_argument("--job-id")
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--steps", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.steps <= 1000:
        parser.error("steps must be between 1 and 1000")
    if args.retry and not (args.job_id and args.execute):
        parser.error("retry requires execute and job-id")
    if args.execute and not (args.job_id or (args.approved_report and args.backup and args.version_id)):
        parser.error("execute requires approved-report, backup and explicit version-id, or an accepted job-id")
    settings = get_settings().model_copy(update={"model_calls_enabled": False})
    sessions = make_sessions(settings.database_url, workspace_root=settings.workspace_root)
    sessions.configure(info={**sessions.kw["info"], "source_store": FileStore(settings.source_storage_root)})
    cleaner = LegacyHtmlCleaner(sessions)
    try:
        if not args.execute:
            result = cleaner.jobs.get(args.job_id) if args.job_id else cleaner.preview(after=args.after, limit=args.limit)
        else:
            if args.job_id:
                job = cleaner.jobs.get(args.job_id)
                if job["target_kind"] != "html_original":
                    parser.error("job-id is not a legacy HTML cleanup")
                if args.retry:
                    cleaner.jobs.retry(args.job_id)
                ids = [args.job_id]
            else:
                report = json.loads(args.approved_report.read_text(encoding="utf-8"))
                if report.get("database") != sessions.kw["bind"].url.database:
                    parser.error("approved report belongs to another database")
                entries = {item["version_id"]: item for item in report["items"]}
                if len(args.version_id) > 500 or any(identity not in entries for identity in args.version_id):
                    parser.error("version-id must select at most 500 versions in the approved report")
                ids = [cleaner.admit(entries[identity], backup_folder=args.backup)["id"] for identity in dict.fromkeys(args.version_id)]
            for identity in ids:
                for _ in range(args.steps):
                    job = cleaner.jobs.get(identity)
                    if job["status"] in {"completed", "failed"}:
                        break
                    if cleaner.step(identity=identity) is None:
                        break
            result = {"jobs": [cleaner.jobs.get(identity) for identity in ids]}
        content = json.dumps(result, ensure_ascii=False, default=str, indent=2)
        if args.output:
            args.output.write_text(content, encoding="utf-8")
        print(content)
    finally:
        sessions.kw["bind"].dispose()


if __name__ == "__main__":
    main()
