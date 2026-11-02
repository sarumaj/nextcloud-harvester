# The External App's image: Python with the app, and frpc, which connects it to HaRP
# (from Nextcloud's app-skeleton-python).
#############################
# Stage 1: Builder
#############################
FROM python:3.12-slim-bookworm AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies using a cache mount.
COPY requirements.txt /
RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -m pip install --root-user-action=ignore -r requirements.txt && rm requirements.txt

# Download and install FRP client with checksum verification
# FRP version and checksums - update these when upgrading
ARG FRP_VERSION=0.71.1
ARG FRP_AMD64_SHA256=1d9875210972a5d1a62b2dc4c48df45ab0af05d3ca1b5e494a014428259df803
ARG FRP_ARM64_SHA256=8edf590b0c396fa42f9c147121a76af83cfc2002b08f5aef0ea1008c43482019

RUN set -ex; \
    ARCH=$(uname -m); \
    if [ "$ARCH" = "aarch64" ]; then \
        FRP_ARCH="arm64"; \
        FRP_SHA256="${FRP_ARM64_SHA256}"; \
    else \
        FRP_ARCH="amd64"; \
        FRP_SHA256="${FRP_AMD64_SHA256}"; \
    fi; \
    FRP_URL="https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${FRP_ARCH}.tar.gz"; \
    echo "Downloading FRP v${FRP_VERSION} for ${FRP_ARCH}..."; \
    curl -fsSL "${FRP_URL}" -o /tmp/frp.tar.gz; \
    ACTUAL_SHA256=$(sha256sum /tmp/frp.tar.gz | cut -d' ' -f1); \
    if [ "$ACTUAL_SHA256" != "$FRP_SHA256" ]; then \
        echo "Checksum verification failed for FRP v${FRP_VERSION} (${FRP_ARCH})"; \
        echo "Expected: ${FRP_SHA256}"; \
        echo "Got:      ${ACTUAL_SHA256}"; \
        exit 1; \
    fi; \
    tar -C /tmp -xzf /tmp/frp.tar.gz; \
    cp /tmp/frp_${FRP_VERSION}_linux_${FRP_ARCH}/frpc /usr/local/bin/frpc; \
    chmod +x /usr/local/bin/frpc; \
    rm -rf /tmp/frp_${FRP_VERSION}_linux_${FRP_ARCH} /tmp/frp.tar.gz; \
    echo "FRP client installed successfully"

# rclone, for the source of file stores and cloud drives (sources/remote.py), checked like frpc.
# The zip is opened with Python's zipfile: the build stage has no unzip.
ARG RCLONE_VERSION=1.75.2
ARG RCLONE_AMD64_SHA256=349ac8fba6ff65d6247043f1750cdcb518ec5d500ef91463a10d37c0ccdf3702
ARG RCLONE_ARM64_SHA256=7e1e8d69654941b7b7df84ee74c5f7fb09cce7d5496947fb7bf16b55ff427d10

RUN set -ex; \
    if [ "$(uname -m)" = "aarch64" ]; then RCLONE_ARCH=arm64; RCLONE_SHA256="${RCLONE_ARM64_SHA256}"; \
    else RCLONE_ARCH=amd64; RCLONE_SHA256="${RCLONE_AMD64_SHA256}"; fi; \
    curl -fsSL "https://github.com/rclone/rclone/releases/download/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${RCLONE_ARCH}.zip" \
        -o /tmp/rclone.zip; \
    echo "${RCLONE_SHA256}  /tmp/rclone.zip" | sha256sum -c -; \
    python3 -c "import zipfile, sys; z = zipfile.ZipFile('/tmp/rclone.zip'); \
open('/usr/local/bin/rclone', 'wb').write(z.read(next(n for n in z.namelist() if n.endswith('/rclone'))))"; \
    chmod +x /usr/local/bin/rclone; \
    rm /tmp/rclone.zip; \
    /usr/local/bin/rclone version | head -1

#############################
# Stage 2: Final Runtime Image
#############################
FROM python:3.12-slim-bookworm

# Copy installed Python packages and FRP client from the builder.
COPY --from=builder /usr/local/ /usr/local/

# pgrep for the health check; ffmpeg for yt-dlp, which joins a video's separate video and audio
# streams with it (the best formats of YouTube and most sites come so).
RUN apt-get update && apt-get install -y --no-install-recommends procps ffmpeg && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# yt-dlp starts ffmpeg and ffprobe through these (sources/media.py, FFMPEG): without the app's
# environment, which holds its secret, as the browser and the other helpers run (child_env); a
# media file crafted against ffmpeg would find nothing to take.
RUN mkdir -p /usr/local/lib/harvester-ffmpeg && \
    for tool in ffmpeg ffprobe; do \
        printf '#!/bin/sh\nexec env -i PATH=/usr/bin:/bin HOME="$HOME" LANG=C.UTF-8 /usr/bin/%s "$@"\n' "$tool" \
            > /usr/local/lib/harvester-ffmpeg/$tool; \
        chmod 755 /usr/local/lib/harvester-ffmpeg/$tool; \
    done && \
    /usr/local/lib/harvester-ffmpeg/ffmpeg -version | head -1

# Chromium for the recorder and the browser source, at Playwright's own version, with the
# libraries it needs: the full browser, run in its new headless mode (browser.CHANNEL), not
# the headless shell, which sites' bot protection tells from Chrome (about 150 MB more).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# The unprivileged user the app runs as (start.sh drops to it).
RUN useradd --system --uid 10001 --user-group --create-home --home-dir /home/harvester harvester

# The app.
COPY ex_app/img /ex_app/img
COPY ex_app/js /ex_app/js
COPY ex_app/lib /ex_app/lib

# Copy scripts with the proper permissions.
COPY --chmod=775 healthcheck.sh /
COPY --chmod=775 start.sh /

# Set working directory and define entrypoint/healthcheck.
WORKDIR /ex_app/lib
ENTRYPOINT ["/start.sh", "python3", "main.py"]
HEALTHCHECK --interval=2s --timeout=2s --retries=300 CMD /healthcheck.sh
