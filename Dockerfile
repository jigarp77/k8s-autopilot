# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="K8s Autopilot"
LABEL org.opencontainers.image.description="AI-powered Kubernetes remediation operator"
LABEL org.opencontainers.image.source="https://github.com/YOUR_ORG/k8s-autopilot"

RUN groupadd --gid 2000 autopilot \
    && useradd --uid 1000 --gid 2000 --shell /bin/false autopilot

RUN mkdir -p /data && chown autopilot:autopilot /data

WORKDIR /app

COPY --from=builder /install /usr/local

COPY --chown=autopilot:autopilot autopilot/ ./autopilot/

USER autopilot

EXPOSE 8000 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"

CMD ["python", "-m", "autopilot.main"]
