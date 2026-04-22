# ============================================================
# Stage 1: Build -- install dependencies into a virtualenv
# ============================================================
FROM registry.access.redhat.com/ubi9/python-312:1 AS builder

USER 0
WORKDIR /build

COPY requirements.txt .
RUN python3 -m venv /build/venv && \
    /build/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /build/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY src/ src/

# ============================================================
# Stage 2: Runtime -- minimal Red Hat UBI image
# ============================================================
FROM registry.access.redhat.com/ubi10/python-312-minimal:10.1-1776702183

LABEL name="mcp-dcr-proxy" \
      summary="MCP DCR Proxy with OAuth Dynamic Client Registration" \
      description="OAuth gateway with DCR (RFC 7591) for MCP servers via Red Hat SSO" \
      maintainer="graphql-dev-team@redhat.com"

WORKDIR /app

COPY --from=builder /build/venv /app/venv
COPY --from=builder /build/src /app/src
COPY config.example.json /app/config.example.json

ENV PYTHONPATH=/app/src \
    PATH="/app/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8080

USER 1001

ENTRYPOINT ["python", "-m", "mcp_proxy"]
CMD ["--config", "/app/config/config.json"]
