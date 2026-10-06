# Isolated runtime for the `claude_code` LLM provider (personal/dev use only).
# The container has no direct internet: its only network is internal, and all HTTPS goes through
# the allowlisting proxy (see ../proxy/squid.conf). Claude Code runs with every tool and MCP
# server disabled; see spike.sh for the exact flags and the fail-closed checks.
FROM node:22-slim

ARG CLAUDE_CODE_VERSION=latest
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates jq \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

USER node
WORKDIR /home/node/work

ENV CLAUDE_CONFIG_DIR=/home/node/.claude-isolated \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    HTTPS_PROXY=http://llm-proxy:3128 \
    HTTP_PROXY=http://llm-proxy:3128

COPY --chown=node:node spike.sh /home/node/spike.sh
CMD ["sleep", "infinity"]
