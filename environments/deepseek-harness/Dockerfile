# Prepared environment for DeepSeek Harness (dsh) — https://github.com/deepseek-ai/deepseek-harness
# dsh needs Node >= 24 (zstd zlib exports, Promise.withResolvers); this image carries it so
# the host toolchain does not matter.
FROM node:24-slim
RUN npm install -g @deepseek-ai/dsh@0.1.0-rc.8 && npm cache clean --force
# /workspace is the harness the agent evolves; /state holds dsh sessions/profiles.
ENV DSH_HOME=/state
ENV DSH_TELEMETRY_MODE=DISABLED
WORKDIR /workspace
ENTRYPOINT ["dsh"]
