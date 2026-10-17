"""稳定的 Uvicorn 入口；HTTP 装配与路由位于 app 包。"""
from knowledgeops_agent.app.application import create_app as create_app

app = create_app()
