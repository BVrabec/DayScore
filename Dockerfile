# syntax=docker/dockerfile:1
# The base image is pinned by digest, so every build starts from the same bytes. Dependabot
# proposes updates.
ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:2b6e177adb67a564bba97e6f0eab8760a7dc3645582d70277a055a4327a737e5

# ---- rclone (backups to Google Drive, Dropbox, OneDrive): a pinned release, checksum-verified.
FROM ${PYTHON_IMAGE} AS rclone
ARG RCLONE_VERSION=v1.75.1
ARG RCLONE_SHA256_AMD64=982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab
ARG RCLONE_SHA256_ARM64=03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && arch="$(dpkg --print-architecture)" \
    && case "$arch" in amd64) sum="$RCLONE_SHA256_AMD64" ;; arm64) sum="$RCLONE_SHA256_ARM64" ;; \
         *) echo "No rclone checksum for $arch" >&2; exit 1 ;; esac \
    && curl -fsSL "https://downloads.rclone.org/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-${arch}.zip" -o /tmp/rclone.zip \
    && echo "${sum}  /tmp/rclone.zip" | sha256sum -c - \
    && unzip -j /tmp/rclone.zip "*/rclone" -d /out && chmod 755 /out/rclone

# ---- the app
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TMP_DIR=/tmp/dayscore \
    PORT=8000

# Exact versions from requirements.lock, each download checked against its hash. Only
# ready-made wheels (SQLCipher's includes its own encryption library, for amd64 and arm64).
COPY requirements.lock /tmp/
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock
COPY --from=rclone /out/rclone /usr/local/bin/rclone

WORKDIR /app
COPY app ./app
COPY scripts ./scripts

RUN useradd --system --uid 1000 --create-home dayscore \
    && mkdir -p /data && chown dayscore /data
USER dayscore
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"

# Behind a reverse proxy, set FORWARDED_ALLOW_IPS to its address so logins are counted per visitor.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --no-server-header"]
