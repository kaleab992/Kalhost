# KALPRO Hosting Bot - multi-language image for Render
# Runs: Python, Node.js, TypeScript, Shell, PHP, Ruby, Perl, Lua, C/C++ out of the box.
# Go / Java / Rust are opt-in (bigger image): add them in APT_EXTRA, see the comment below.

FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    DATA_DIR=/data

# Node.js + npm copied from the official image (no install script, no extra downloads)
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm install -g tsx

# Extra runtimes. To add Go and Java, use:
#   ARG APT_EXTRA="php-cli ruby lua5.4 build-essential golang-go default-jdk-headless"
ARG APT_EXTRA="php-cli ruby lua5.4 build-essential"
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl unzip procps ca-certificates ${APT_EXTRA} \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY KALREADY.py .
RUN mkdir -p /data

EXPOSE 10000
CMD ["python", "-u", "KALREADY.py"]
