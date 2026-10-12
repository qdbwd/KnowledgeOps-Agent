# Make 只组织 Docker 命令；宿主机不安装 Python/Node 依赖。
.DEFAULT_GOAL := help
.NOTPARALLEL:
DC = docker compose
DEV = docker compose -f compose.yaml -f compose.dev.yaml --profile development
.PHONY: help up dev stop logs status restart-worker
help:
	@echo "make up              Build and run Docker services"
	@echo "make dev             Prepare dev container; then run python scripts/dev.py inside it"
	@echo "make stop            Stop services; preserve data"
	@echo "make logs            Follow API/worker/web logs"
	@echo "make status          List service health"
	@echo "make restart-worker  Production worker only; in dev restart scripts/dev.py"
up:
	$(DC) stop api worker web dev
	$(DC) rm -f init
	$(DC) up -d --build --wait
	$(DC) exec -T worker python scripts/wait_for_worker.py
dev:
	$(DC) stop api worker web dev
	$(DC) rm -f init
	$(DEV) up -d --build --wait
	@echo "Enter the dev container and run: python scripts/dev.py"
stop:
	$(DC) --profile development stop
logs:
	$(DC) logs -f --tail=100 api worker web
status:
	$(DC) --profile development ps
restart-worker:
	$(DC) restart worker
