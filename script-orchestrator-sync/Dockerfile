FROM python:3.13-slim

ARG BLENDER_VERSION=4.2.2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    xz-utils \
    libx11-6 \
    libxext6 \
    libxrender1 \
    libxi6 \
    libxfixes3 \
    libxrandr2 \
    libxxf86vm1 \
    libxkbcommon0 \
    libsm6 \
    libice6 \
    libgl1 \
    libglu1-mesa \
    libfontconfig1 \
    libfreetype6 \
    libasound2 \
    libpulse0 \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    blender_series="$(echo "${BLENDER_VERSION}" | awk -F. '{print $1 "." $2}')"; \
    blender_url="https://download.blender.org/release/Blender${blender_series}/blender-${BLENDER_VERSION}-linux-x64.tar.xz"; \
    curl -fsSL "${blender_url}" -o /tmp/blender.tar.xz; \
    mkdir -p /opt/blender; \
    tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1; \
    ln -sf /opt/blender/blender /usr/local/bin/blender; \
    rm -f /tmp/blender.tar.xz

WORKDIR /workspace/script-orchestrator-sync

COPY script-orchestrator-sync/requirements.txt /workspace/script-orchestrator-sync/requirements.txt
RUN pip install --upgrade pip && pip install -r /workspace/script-orchestrator-sync/requirements.txt

COPY script-orchestrator-sync /workspace/script-orchestrator-sync
COPY vse-stitcher /workspace/vse-stitcher
COPY vse-text /workspace/vse-text

ENV PYTHONPATH=/workspace/script-orchestrator-sync/src \
    BLENDER_BIN=/usr/local/bin/blender \
    FFMPEG_BIN=/usr/bin/ffmpeg \
    VSE_STITCHER_SCRIPT_PATH=/workspace/vse-stitcher/scripts/vse_bpm_preset_deterministic_bl456.py \
    VSE_TEXT_SCRIPT_PATH=/workspace/vse-text/scripts/vse_bpm_preset_deterministic_bl456.py

RUN chmod +x /workspace/script-orchestrator-sync/scripts/container_start.sh \
    /workspace/script-orchestrator-sync/scripts/batch_entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["/workspace/script-orchestrator-sync/scripts/container_start.sh"]
CMD ["uvicorn", "so_sync.api:app", "--host", "0.0.0.0", "--port", "8000"]
