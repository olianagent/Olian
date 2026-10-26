# Olian orchestrator + Hermes Agent (pinned via HERMES_REF) in one image.
# Hermes supports Python 3.14 only and is installed from source (it refuses wheel builds), with the
# `messaging` (API server/gateway) and `mcp` (remote MCP client -> Olian facade) extras.
FROM python:3.14-slim
ARG HERMES_REF=main
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir uv
RUN git clone --depth 1 --branch "${HERMES_REF}" https://github.com/NousResearch/hermes-agent /opt/hermes \
    && uv pip install --system -e "/opt/hermes[messaging,mcp]"
WORKDIR /app
COPY pyproject.toml ./
COPY olian ./olian
RUN pip install --no-cache-dir '.[tguser]'
RUN useradd -m -u 10001 olian && mkdir -p /data && chown olian /data
USER olian
ENV OLIAN_DATA_DIR=/data OLIAN_HOST=0.0.0.0 OLIAN_HERMES_BIN=hermes HOME=/data/home
VOLUME /data
EXPOSE 8600
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8600/health', timeout=4).status == 200 else 1)"
CMD ["olian", "serve", "--start-agents"]
