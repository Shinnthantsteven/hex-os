FROM node:22-slim

# Install curl (health checks) and openclaw
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g openclaw@latest

# Persistent state lives on the Railway volume mounted at /data
ENV OPENCLAW_STATE_DIR=/data/.openclaw \
    OPENCLAW_WORKSPACE_DIR=/data/workspace \
    OPENCLAW_GATEWAY_PORT=8080

EXPOSE 8080

# /healthz = liveness, /readyz = readiness (both provided by openclaw gateway)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -sf http://localhost:8080/healthz || exit 1

# --bind lan  → listen on 0.0.0.0 so Railway's HTTP proxy can reach the port
# --allow-unconfigured → start even if gateway.mode != local (first boot)
# --force     → kill any stale listener from a crashed previous process
CMD ["openclaw", "gateway", "--bind", "lan", "--allow-unconfigured", "--force"]
