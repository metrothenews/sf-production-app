FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/script-orchestrator-sync

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

ENV PYTHONPATH=/workspace/script-orchestrator-sync/src

EXPOSE 8000

CMD ["uvicorn", "so_sync.api:app", "--host", "0.0.0.0", "--port", "8000"]
