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
# urllib3-future (nc_py_api's niquests) built from source with URLLIB3_NO_OVERRIDE: its wheel
# installs itself as urllib3 too, over upstream urllib3, and lacks what gallery-dl calls
# (urllib3.connection.create_urllib3_context); so built, niquests uses urllib3_future and
# gallery-dl (requests) upstream urllib3. tests/test_gallery.py checks it.
RUN --mount=type=cache,target=/root/.cache/pip \
    URLLIB3_NO_OVERRIDE=1 python3 -m pip install --root-user-action=ignore \
    --no-binary=urllib3-future -r requirements.txt && rm requirements.txt

# Patchright (the disguised browser, harvester/browser/engine.py) starts Playwright's Chromium:
# the build fails when their versions want different ones. Its own copy of Node.js (some 120 MB)
# gives way to Playwright's, which runs its driver alike (Node.js 24 both).
RUN set -ex; \
    SITE=$(python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"); \
    python3 -c "import json, sys; revision = lambda package: [b['revision'] for b in json.load(open(f'{sys.argv[1]}/{package}/driver/package/browsers.json'))['browsers'] if b['name'] == 'chromium']; sys.exit(revision('playwright') != revision('patchright'))" "$SITE"; \
    ln -sf "$SITE/playwright/driver/node" "$SITE/patchright/driver/node"; \
    python3 -m patchright --version

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

# AdGuard's dnsproxy, through which the container's lookups go to the admin's DNS servers
# (harvester/dns.py): a static Go program, checked like frpc.
ARG DNSPROXY_VERSION=0.86.0
ARG DNSPROXY_AMD64_SHA256=17ac43f76ebfcedf547c663f6f31aa70e68b8eb6fc41d2fc37d3bfe21a2ced66
ARG DNSPROXY_ARM64_SHA256=eda840d39da0c2777ea2431d3d73407e813e3fc0e411bfe256a9fb4b97d08247

RUN set -ex; \
    if [ "$(uname -m)" = "aarch64" ]; then DNSPROXY_ARCH=arm64; DNSPROXY_SHA256="${DNSPROXY_ARM64_SHA256}"; \
    else DNSPROXY_ARCH=amd64; DNSPROXY_SHA256="${DNSPROXY_AMD64_SHA256}"; fi; \
    curl -fsSL "https://github.com/AdguardTeam/dnsproxy/releases/download/v${DNSPROXY_VERSION}/dnsproxy-linux-${DNSPROXY_ARCH}-v${DNSPROXY_VERSION}.tar.gz" \
        -o /tmp/dnsproxy.tar.gz; \
    echo "${DNSPROXY_SHA256}  /tmp/dnsproxy.tar.gz" | sha256sum -c -; \
    mkdir /tmp/dnsproxy; \
    tar -C /tmp/dnsproxy -xzf /tmp/dnsproxy.tar.gz; \
    install -m 0755 "/tmp/dnsproxy/linux-${DNSPROXY_ARCH}/dnsproxy" /usr/local/bin/dnsproxy; \
    rm -rf /tmp/dnsproxy /tmp/dnsproxy.tar.gz; \
    /usr/local/bin/dnsproxy --version

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
# the headless shell, which sites' bot protection tells from Chrome. The shell is not installed
# (--no-shell: some 270 MB that nothing starts; an empty HARVESTER_BROWSER_CHANNEL means the full
# browser too); Patchright starts this Chromium and installs none of its own.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps --no-shell chromium && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# The unprivileged user the app runs as (start.sh drops to it), and frpc's own: HaRP's key in
# frpc's configuration and environment stays out of the reach of the app's user, as whom the
# browsers and helper programs run.
RUN useradd --system --uid 10001 --user-group --create-home --home-dir /home/harvester harvester && \
    useradd --system --uid 10002 --user-group --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin harvester-frp

# The app.
COPY ex_app/img /ex_app/img
COPY ex_app/js /ex_app/js
COPY ex_app/lib /ex_app/lib
# The app's modules compiled now: the app's user may not write their __pycache__ there, so each
# start of the app compiled them all again, in memory.
RUN python3 -m compileall -q /ex_app/lib

# Copy scripts with the proper permissions.
COPY --chmod=775 healthcheck.sh /
COPY --chmod=775 start.sh /

# Set working directory and define entrypoint/healthcheck.
WORKDIR /ex_app/lib
ENTRYPOINT ["/start.sh", "python3", "main.py"]
# The health check (whether frpc runs) every 15 s, not every 2 s for the container's whole life
# (a bash and a pgrep each time). AppAPI waits for "healthy" after a deploy: through HaRP for 90 s
# (its wait_for_start, 180 times 0.5 s), else for 900 s; the first check, after 15 s, is in time,
# and five more come within HaRP's wait. Failures in the first 5 minutes do not count (start
# period), then 4 in a row (a minute) make it unhealthy. No --start-interval (faster checks while
# starting): Docker before 25 ignores it, and the deploy daemons' Docker is not known.
HEALTHCHECK --interval=15s --timeout=5s --start-period=300s --retries=4 CMD /healthcheck.sh
