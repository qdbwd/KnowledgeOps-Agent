# 基础工具只装在本项目镜像；不会修改 Windows/Conda/共享容器环境。
FROM node:24-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS toolchain
COPY --from=node /usr/local/ /usr/local/
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
    fonts-noto-cjk libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libx11-6 libxcomposite1 libxdamage1 libxext6 \
    libxfixes3 libxrandr2 libgbm1 libxcb1 libxkbcommon0 libpango-1.0-0 \
    libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*
ENV UV_PROJECT_ENVIRONMENT=/workspace/.venv \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/workspace/.venv/bin:$PATH"
WORKDIR /workspace

# 锁文件生成后可构建不可变应用镜像；开发模式用同一工具链和锁文件。
FROM toolchain AS application
COPY pyproject.toml uv.lock /workspace/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-install-project --no-dev
COPY README.md /workspace/
COPY backend/src /workspace/backend/src
COPY skill-seeds /workspace/skill-seeds
COPY config /workspace/config
COPY scripts /workspace/scripts
COPY alembic.ini /workspace/
COPY migrations /workspace/migrations
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
