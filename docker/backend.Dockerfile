FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts
COPY eval_sets ./eval_sets
COPY queryagent_roles.yaml ./queryagent_roles.yaml

RUN pip install --upgrade pip \
    && pip install ".[postgres,web]"

EXPOSE 8000

CMD ["uvicorn", "queryagent.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
