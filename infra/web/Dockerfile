FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    STUDIO_WEB_DATA_DIR=/data/web-studio \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 STUDIO_ALLOW_MODEL_DOWNLOAD=0 \
    OMP_NUM_THREADS=3 OPENBLAS_NUM_THREADS=3 MKL_NUM_THREADS=3
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-dejavu-core espeak-ng tini ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*
ARG OPENCODE_VERSION=1.19.4
RUN npm install --prefix /opt/opencode --global --ignore-scripts opencode-ai@${OPENCODE_VERSION} \
    && node /opt/opencode/lib/node_modules/opencode-ai/postinstall.mjs \
    && /opt/opencode/bin/opencode --version \
    && npm cache clean --force
ENV PATH="/opt/opencode/bin:${PATH}"
WORKDIR /app
COPY infra/web/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml /app/
COPY studio /app/studio
COPY assets/fonts/kids /app/assets/fonts/kids
RUN pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home studio \
    && mkdir -p /data/web-studio /app/output \
    && chown -R studio:studio /data /app/output
USER studio
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "studio.web", "--host", "0.0.0.0", "--port", "8080"]
