# syntax=docker/dockerfile:1
# =============================================================================
#  WastedDailyVI – Docker-Image mit NVIDIA-GPU (NVENC-Encoding, Whisper auf CUDA)
#
#  Bauen:      docker compose build                     (oder: docker build -t wasteddailyvi .)
#  Updaten:    sh scripts/update.sh   (baut mit --build-arg REFRESH=… -> neueste yt-dlp/Claude-CLI)
#  Claude-CLI: WITH_CLAUDE_CLI=1 in .env, dann docker compose build (torch wird dabei NICHT neu geladen)
#  Nur CPU:    TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu in .env (siehe README, "Ohne GPU")
#
#  CUDA 12.8 + PyTorch cu128: läuft auf allen RTX-Karten von 20xx bis 50xx (Blackwell, sm_120);
#  braucht NVIDIA-Treiber 570 oder neuer.
# =============================================================================
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
    HF_HOME=/app/data/cache/huggingface \
    CLAUDE_CONFIG_DIR=/home/app/.claude \
    DISABLE_AUTOUPDATER=1 \
    WDVI_HOST=0.0.0.0 \
    TZ=Etc/UTC

# System: Python 3.12, ffmpeg (Ubuntu-Build mit NVENC + libass), Cairo (Emoji-SVGs), Schriften, curl
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv \
        ffmpeg libcairo2 fontconfig fonts-dejavu-core \
        curl ca-certificates tini tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv

WORKDIR /app

# 1) Python-Abhängigkeiten (Layer bleibt gecacht, solange pyproject.toml und TORCH_INDEX_URL gleich sind).
#    torch zuerst vom PyTorch-Index (CUDA-12.8-Build), danach der Rest inkl. Extra "music".
#    Wichtig: ARGs, die sich ändern (WITH_CLAUDE_CLI, REFRESH, APP_UID), erst NACH diesem Layer deklarieren –
#    sonst lädt jede Änderung torch (~3 GB) neu.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
COPY pyproject.toml README.md ./
RUN pip install --upgrade pip \
    && pip install --index-url "${TORCH_INDEX_URL}" torch \
    && python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
print('\n'.join(p['dependencies'] + p['optional-dependencies']['music']))" > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# 2) Optional: Node.js für die Claude-Code-CLI (llm.backend: cli). Eigener Layer -> Umschalten baut torch nicht neu.
ARG WITH_CLAUDE_CLI=0
RUN if [ "$WITH_CLAUDE_CLI" = "1" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends nodejs npm \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# 3) Schnell veraltende Teile: yt-dlp (Extraktoren brechen regelmäßig), dessen JavaScript-Laufzeit (Deno +
#    yt-dlp-ejs, nötig für YouTube) und die Claude-CLI. scripts/update.sh setzt REFRESH auf die aktuelle Zeit
#    -> nur dieser Layer (und die kleinen danach) wird neu gebaut. pyproject.toml verlangt schon
#    "yt-dlp[default]>=2025.10.22" (inkl. yt-dlp-ejs, dem JS-Challenge-Löser); hier kommt das Extra "deno"
#    (Deno-Laufzeit als pip-Paket, landet in /opt/venv/bin) dazu, jeweils in der neuesten Version.
ARG REFRESH=0
RUN echo "refresh ${REFRESH}" \
    && pip install --upgrade "yt-dlp[default,deno]" \
    && (deno --version || echo "WARNUNG: deno fehlt - YouTube-Downloads liefern evtl. weniger Formate.") \
    && if [ "$WITH_CLAUDE_CLI" = "1" ]; then \
        npm install -g @anthropic-ai/claude-code@latest && npm cache clean --force; \
    fi

# 4) Nicht-root-Benutzer "app". APP_UID = deine Benutzer-ID auf dem Host (Linux: 'id -u'), damit dir die
#    Dateien in ./data gehören. Das Ubuntu-Image bringt "ubuntu" mit UID 1000 mit -> entfernen.
ARG APP_UID=1000
RUN (userdel -r ubuntu 2>/dev/null || true) \
    && (getent group "${APP_UID}" >/dev/null || groupadd --gid "${APP_UID}" app) \
    && useradd --create-home --uid "${APP_UID}" --gid "${APP_UID}" --shell /bin/bash app

# 5) Programm + Assets. Editierbar installiert, damit assets/ im Projektordner gefunden wird.
COPY src ./src
COPY assets ./assets
COPY scripts ./scripts
COPY config.example.yaml .env.example ./
RUN pip install --no-deps -e .

# 6) Gesichts-Modell (YuNet) schon beim Bauen nach assets/models laden (im Image; data/ ist zur Laufzeit ein
#    Volume und würde es verdecken). Scheitert das, lädt "wdvi init" es nach data/cache/models. Gesucht wird
#    in dieser Reihenfolge: assets/models, data/cache/models, data/cache (PathsConfig.face_model_paths).
ARG YUNET_URL=https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
ARG YUNET_MIRROR=https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx
RUN mkdir -p assets/models && cd assets/models \
    && for url in "${YUNET_URL}" "${YUNET_MIRROR}"; do \
         curl -fsSL --retry 3 -o face_detection_yunet_2023mar.onnx "$url" \
         && [ "$(stat -c %s face_detection_yunet_2023mar.onnx)" -gt 100000 ] && break; \
         rm -f face_detection_yunet_2023mar.onnx; \
       done \
    && ( [ -f face_detection_yunet_2023mar.onnx ] \
         || echo "WARNUNG: YuNet-Modell konnte nicht geladen werden - 'wdvi init' versucht es erneut." )

RUN sed -i 's/\r$//' scripts/*.sh \
    && chmod +x scripts/*.sh \
    && mkdir -p /app/data /home/app/.claude \
    && chown -R app:"$(id -g app)" /app /home/app

EXPOSE 8420

# Läuft als Benutzer "app" (UID = APP_UID, nicht root) – auch "docker compose exec …", damit in ./data keine
# root-Dateien entstehen. ./data und ./claude-home vorher anlegen (mkdir -p data claude-home).
USER app
ENTRYPOINT ["/usr/bin/tini", "--", "/app/scripts/docker-entrypoint.sh"]
CMD ["wdvi", "run"]
