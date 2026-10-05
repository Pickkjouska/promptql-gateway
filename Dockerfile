# PromptQL 反代网关 —— Linux 部署镜像
# 基础镜像同时满足: 服务运行 + Camoufox（注册环节需要浏览器过 reCAPTCHA）
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_DATA_DIR=/data \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080

WORKDIR /srv/app

# Camoufox 运行所需的系统库（Firefox 内核依赖）+ 构建工具
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils \
        libgtk-3-0 libasound2 libdbus-glib-1-2 libx11-xcb1 libxtst6 \
        libxrandr2 libxcomposite1 libxdamage1 libxfixes3 libxrender1 \
        libpango-1.0-0 libcairo2 libnss3 libnspr4 \
        libfontconfig1 fonts-liberation fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-camoufox.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir -r requirements-camoufox.txt \
 && python -m camoufox fetch || true

COPY app ./app
COPY web ./web
COPY scripts ./scripts
COPY deploy ./deploy

RUN mkdir -p /data && useradd -m -u 10001 appuser && chown -R appuser:appuser /srv/app /data
USER appuser

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
