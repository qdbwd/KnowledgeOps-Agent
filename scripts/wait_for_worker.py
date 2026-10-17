"""启动脚本等待本项目真实Celery消费者就绪，避免过早宣称应用可以接单。"""

import time

from knowledgeops_agent.app.runs.queue import create_queue
from knowledgeops_agent.core.config import Settings


def main():
    queue = create_queue(Settings(model_calls_enabled=False).celery_broker_url)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if queue.control.ping(timeout=1):
                print("Project worker is ready; startup recovery scan has finished.")
                return
            time.sleep(0.5)
        raise RuntimeError(
            "Project worker did not become ready within 120 seconds; inspect worker logs"
        )
    finally:
        queue.close()


if __name__ == "__main__":
    main()
