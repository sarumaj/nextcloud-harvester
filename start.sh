#!/bin/bash
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

set -e

# Only create a config file if HP_SHARED_KEY is set.
if [ -n "$HP_SHARED_KEY" ]; then
    echo "HP_SHARED_KEY is set, creating /frpc.toml configuration file..."
    if [ -d "/certs/frp" ]; then
        echo "Found /certs/frp directory. Creating configuration with TLS certificates."
        cat <<EOF > /frpc.toml
serverAddr = "$HP_FRP_ADDRESS"
serverPort = $HP_FRP_PORT
loginFailExit = false

transport.tls.enable = true
transport.tls.certFile = "/certs/frp/client.crt"
transport.tls.keyFile = "/certs/frp/client.key"
transport.tls.trustedCaFile = "/certs/frp/ca.crt"
transport.tls.serverName = "harp.nc"

metadatas.token = "$HP_SHARED_KEY"

[[proxies]]
remotePort = $APP_PORT
type = "tcp"
name = "$APP_ID"
[proxies.plugin]
type = "unix_domain_socket"
unixPath = "/tmp/exapp.sock"
EOF
    else
        echo "Directory /certs/frp not found. Creating configuration without TLS certificates."
        cat <<EOF > /frpc.toml
serverAddr = "$HP_FRP_ADDRESS"
serverPort = $HP_FRP_PORT
loginFailExit = false

transport.tls.enable = false

metadatas.token = "$HP_SHARED_KEY"

[[proxies]]
remotePort = $APP_PORT
type = "tcp"
name = "$APP_ID"
[proxies.plugin]
type = "unix_domain_socket"
unixPath = "/tmp/exapp.sock"
EOF
    fi
else
    echo "HP_SHARED_KEY is not set. Skipping FRP configuration."
fi

# The app runs as the unprivileged user harvester (the image's), frpc as harvester-frp, both
# without a way back to root (no_new_privs): a page that broke out of Chromium, which has no
# sandbox in a container, owns no more than harvester, which may neither read frpc's
# configuration (HaRP's key) nor its environment. frpc reaches the app as before, on its socket
# (/tmp/exapp.sock, which uvicorn makes 0666). Root only writes frpc's configuration (above) and
# hands the app's volume to the user (the volume of an earlier version is root's). An image
# without harvester-frp runs frpc as harvester.
AS_APP=""
AS_FRP=""
if [ "$(id -u)" = 0 ] && id harvester >/dev/null 2>&1; then
    FRP_USER=harvester
    if id harvester-frp >/dev/null 2>&1; then
        FRP_USER=harvester-frp
    fi
    if [ -f /frpc.toml ]; then
        chgrp "$FRP_USER" /frpc.toml
        chmod 0640 /frpc.toml
    fi
    if [ -n "${APP_PERSISTENT_STORAGE:-}" ]; then
        mkdir -p "$APP_PERSISTENT_STORAGE"
        chown -R harvester:harvester "$APP_PERSISTENT_STORAGE"
    fi
    AS_APP="setpriv --reuid=harvester --regid=harvester --init-groups --no-new-privs -- env HOME=/home/harvester"
    AS_FRP="setpriv --reuid=$FRP_USER --regid=$FRP_USER --init-groups --no-new-privs --"
fi

# If we have a configuration file and the shared key is present, start the FRP client
if [ -f /frpc.toml ] && [ -n "$HP_SHARED_KEY" ]; then
    echo "Starting frpc in the background..."
    # shellcheck disable=SC2086
    $AS_FRP frpc -c /frpc.toml &
fi

# Start the main application (launch cmd for ExApp is an argument for this script)
echo "Starting application: $*"
# shellcheck disable=SC2086
exec $AS_APP "$@"
