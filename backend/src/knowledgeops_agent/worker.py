"""稳定的 python -m 入口；后台执行生命周期位于 app.runs。"""
from knowledgeops_agent.app.runs.worker import main

if __name__ == "__main__":
    main()
