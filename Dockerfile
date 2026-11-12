FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/projects/codepilot/src:/app/projects/agentops/src:/app/shared/src \
    CODEPILOT_SESSION_DB=/data/approvals.sqlite3

WORKDIR /app

COPY pyproject.toml README.md ./
COPY shared ./shared
COPY projects ./projects

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[api,worker,codepilot]" \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8000

CMD ["uvicorn", "codepilot.server:app", "--host", "0.0.0.0", "--port", "8000"]
