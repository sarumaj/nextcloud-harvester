#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Harvester on a real Nextcloud, after the shared action nextcloud-app-server of
# dawid/ci-actions registered its image with AppAPI there (CI's job server): what people
# get, where pytest has fakes of Nextcloud. Harvester's page and API answer through
# AppAPI's proxy for the admin, its admin settings and its occ command are registered, and
# a website job run against the fake site (tests/fake_site.py, in a container of its own)
# saves its files into the admin's files over Nextcloud's WebDAV, tagged with Nextcloud's
# tags. The recorder's browser (Chromium behind the guard) starts and opens the fake site,
# its WebSocket reached on the app's container with AppAPI's headers: without HaRP,
# Nextcloud's proxy does not carry WebSockets. The action passes OCC, NEXTCLOUD_URL,
# NEXTCLOUD_USER, NEXTCLOUD_PASSWORD, EXAPP_CONTAINER, EXAPP_IMAGE and NEXTCLOUD_NETWORK.
#   tests/server/check.sh
set -euo pipefail
: "${OCC:?}" "${NEXTCLOUD_URL:?}" "${NEXTCLOUD_USER:?}" "${NEXTCLOUD_PASSWORD:?}"
: "${EXAPP_CONTAINER:?}" "${EXAPP_IMAGE:?}" "${NEXTCLOUD_NETWORK:?}"
admin=(-u "$NEXTCLOUD_USER:$NEXTCLOUD_PASSWORD")
app="$NEXTCLOUD_URL/index.php/apps/app_api/proxy/harvester"
dav="$NEXTCLOUD_URL/remote.php/dav/files/$NEXTCLOUD_USER"
folder=HarvesterCI
site="$NEXTCLOUD_NETWORK-site"
body="$(mktemp)"

cleanup() {
  rm -f "$body"
  docker rm -f "$site" >/dev/null 2>&1 || true
  docker network disconnect "$site" "$EXAPP_CONTAINER" >/dev/null 2>&1 || true
  docker network rm "$site" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Expects the status $1 for curl's other arguments; the answer is left in $body.
expect() {
  local want="$1" got
  shift
  got="$(curl -s -o "$body" -w '%{http_code}' "$@")"
  if [[ $got != "$want" ]]; then
    echo "::error::${*: -1} answered $got, not $want: $(head -c 500 "$body")"
    exit 1
  fi
  echo "OK: ${*: -1} answers $want"
}

# Fails unless $body contains $1.
contains() {
  grep -qF -- "$1" "$body" || { echo "::error::The answer lacks $1: $(head -c 500 "$body")"; exit 1; }
}

# The page in Nextcloud's top menu, with its script, which AppAPI's proxy serves.
expect 200 "${admin[@]}" "$NEXTCLOUD_URL/index.php/apps/app_api/embedded/harvester/harvester"
contains 'harvester/js/harvester-main.js'
expect 200 "${admin[@]}" "$app/js/harvester-main.js"
expect 200 "${admin[@]}" "$app/api/sources"
jq -e 'any(.[]; .name == "site")' "$body" >/dev/null || { echo "::error::No website source in /api/sources"; exit 1; }
# Without a signed-in person AppAPI's proxy refuses the API (404: it does not say the route
# exists), never handing it to the app.
expect 404 "$app/api/jobs"

# The admin's settings, both forms (declarative settings), as the settings page reads them.
expect 200 "${admin[@]}" -H 'OCS-APIRequest: true' "$NEXTCLOUD_URL/ocs/v2.php/settings/api/declarative/forms?format=json"
for form in admin dns; do
  jq -e --arg form "$form" 'any(.ocs.data[]; .app == "harvester" and .id == $form)' "$body" >/dev/null \
    || { echo "::error::The settings form $form of harvester is not registered"; exit 1; }
done
echo "OK: the admin settings' forms admin and dns are registered"

"$OCC" list harvester | grep -q 'harvester:library-reset' \
  || { echo "::error::occ lacks harvester:library-reset"; exit 1; }
echo "OK: occ has harvester:library-reset"

# The fake site at an address the app counts as public (its guard and checks refuse private
# ones): a subnet of 192.88.99.0/24, the retired 6to4 relays' (RFC 7526), which nothing on
# the job's network uses. Two runs on one Docker daemon each take a /28 of their own.
offset="$(( $(cksum <<<"$NEXTCLOUD_NETWORK" | cut -d' ' -f1) % 16 ))"
for ((try = 0; try < 16; try++)); do
  net="192.88.99.$(( (offset + try) % 16 * 16 ))"
  docker network create --subnet "$net/28" "$site" >/dev/null 2>&1 && break
  net=""
done
[[ -n $net ]] || { echo "::error::No subnet of 192.88.99.0/24 is free for the fake site"; exit 1; }
address="${net%.*}.$(( ${net##*.} + 10 ))"
docker create --name "$site" --network "$site" --ip "$address" --entrypoint python3 "$EXAPP_IMAGE" -c '
import http.server, sys
sys.path.insert(0, "/tmp")
from fake_site import Site
SITE = Site()
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        status, headers, content = SITE.answer("http://site" + self.path)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)
http.server.ThreadingHTTPServer(("", 8000), Handler).serve_forever()
' >/dev/null
docker cp tests/fake_site.py "$site:/tmp/fake_site.py" >/dev/null
docker start "$site" >/dev/null
docker network connect "$site" "$EXAPP_CONTAINER"
start="http://$address:8000/list"
# Listening before the job's run asks it, from the app's container.
for ((try = 1; ; try++)); do
  docker exec "$EXAPP_CONTAINER" python3 -c "import urllib.request; urllib.request.urlopen('$start', timeout=2)" \
    2>/dev/null && break
  ((try < 15)) || { echo "::error::The fake site does not answer at $start"; docker logs "$site"; exit 1; }
  sleep 1
done
echo "OK: the fake site answers the app at $start"

# A website job: the fake site's first listing page, tagged. Its two PDFs are alike: the
# second is a duplicate, not saved again.
recipe="$(jq -cn --arg start "$start" '{start: $start, links: {selector: "a.file", pattern: "\\.pdf$"}}')"
job="$(jq -cn --arg recipe "$recipe" --arg target "/$folder" \
  '{name: "CI", source: "site", target: $target, schedule: "0 6 * * *", options: {recipe: $recipe}, tags: ["harvester-ci"]}')"
expect 200 "${admin[@]}" -H 'Content-Type: application/json' -d "$job" "$app/api/jobs"
id="$(jq -r .id "$body")"
expect 200 "${admin[@]}" -X POST "$app/api/jobs/$id/run"
status=""
for ((try = 0; try < 60; try++)); do
  sleep 2
  curl -s -o "$body" "${admin[@]}" "$app/api/jobs/$id"
  status="$(jq -r '.last_status // ""' "$body")"
  [[ -n $status ]] && break
done
[[ $status == ok ]] || {
  echo "::error::The job's run ended ${status:-not in 2 minutes}: $(jq -r '.last_message // ""' "$body")"
  curl -s "${admin[@]}" "$app/api/log?job=$id" | jq -r '.lines[]?.message' | tail -20
  exit 1
}
echo "OK: the website job ran: $(jq -r .last_message "$body")"

# The files in the admin's folder, over WebDAV, with the fake site's content and the tag.
expect 207 "${admin[@]}" -X PROPFIND -H 'Depth: 1' -H 'Content-Type: application/xml' --data \
  '<d:propfind xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns"><d:prop><d:getcontentlength/><nc:system-tags/></d:prop></d:propfind>' \
  "$dav/$folder/"
contains "/$folder/1-1.pdf"
contains 'harvester-ci'
echo "OK: the folder holds 1-1.pdf, tagged harvester-ci"
expect 200 "${admin[@]}" "$dav/$folder/1-1.pdf"
python3 -c 'import sys; sys.path.insert(0, "tests"); import fake_site; sys.stdout.buffer.write(fake_site.PDF)' \
  | cmp -s - "$body" || { echo "::error::1-1.pdf is not the fake site's PDF"; exit 1; }
echo "OK: 1-1.pdf is the fake site's PDF"

# The recorder: its browser starts in the image and opens the fake site, the screen comes.
docker exec -i -e START="$start" "$EXAPP_CONTAINER" python3 - <<'EOF'
import asyncio, base64, json, os
import websockets

async def record():
    auth = base64.b64encode(f"admin:{os.environ['APP_SECRET']}".encode()).decode()
    headers = {"EX-APP-ID": os.environ["APP_ID"], "EX-APP-VERSION": os.environ["APP_VERSION"],
               "AUTHORIZATION-APP-API": auth}
    url = f"ws://127.0.0.1:{os.environ['APP_PORT']}/ws/record"
    async with websockets.connect(url, additional_headers=headers, max_size=None) as socket:
        await socket.send(json.dumps({"type": "goto", "url": os.environ["START"]}))
        seen, framed = "", False
        while not (seen == os.environ["START"] and framed):
            message = await socket.recv()
            if isinstance(message, bytes):
                framed = True
                continue
            event = json.loads(message)
            if event.get("type") == "error":
                raise SystemExit(f"::error::The recorder said: {event.get('message')}")
            if event.get("type") == "url":
                seen = event["url"]

asyncio.run(asyncio.wait_for(record(), 120))
print("OK: the recorder's browser opened the fake site and sent its screen")
EOF
