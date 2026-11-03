# Harvester: administrator's guide

Installing Harvester, its settings, the browsers' network guard and the backup. What people do
with it is in the [user's guide](user.md).

## Installing

Harvester is an External App: AppAPI runs its image on a deploy daemon (HaRP, which also
carries the recorder's WebSocket). The image is released on ghcr.io as `ghcr.io/sarumaj/harvester`
(the registry and image `appinfo/info.xml` names) and on Forgejo as
`git.ciepiela.org/dawid/harvester`; how a release is made is in the
[developer's guide](developer.md#releases).

**Taking the image from Forgejo instead of ghcr.io:** AppAPI pulls from the one registry in
`info.xml` and has no fallback of its own, but a deploy daemon can map a registry to another,
replacing only that part of the image's name. That is why the registry part holds the owner:

```bash
occ app_api:daemon:registry:add <daemon> --registry-from=ghcr.io/sarumaj --registry-to=git.ciepiela.org/dawid
```

The homelab registers the app with its own local image (`local.homelab/harvester`) and needs
neither.

**Translations** (German and Polish; the page, the settings below and the notifications):
installed from the app store, AppAPI copies the release archive's `l10n/` into the server's
writable apps folder (`<apps>/harvester/l10n/`), where Nextcloud finds them. An app registered
by hand (`occ app_api:app:register` with `--json-info` or `--info-xml`) gets them only when its
JSON names a folder the Nextcloud server can read, `"translations_folder": "/path/to/l10n"`
(the repository's or the archive's `l10n/` at the same version); otherwise, copy that folder to
`<apps>/harvester/l10n/` after each update. Without them everything is English.

## Settings

- **Admin settings:** *Administration settings > Additional settings > Harvester* sets the
  numbers of jobs and browsers at once (1 to 8 each), the proxy, and the **Browser network
  guard** (on by default; see *Safety*); the environment's values (`HARVESTER_RUNNERS`,
  `HARVESTER_BROWSERS`, `HARVESTER_PROXY`, `HARVESTER_PROXY_NAME`, `HARVESTER_GUARD`: off with
  `0`, `false` or `off`) are the defaults. The app reads them each minute, so a change needs no
  restart: a lowered number lets the running jobs and open browsers finish first, and the guard's
  setting applies to the browsers started afterwards. Switch the guard off only when the External
  Apps' network is already firewalled from private networks and the cloud's metadata service.
- **Runners:** `HARVESTER_RUNNERS` jobs run at once (2 by default), each in a thread of its own;
  a job never runs twice at once, and jobs of the same site (the host of the job's address) wait
  for each other, so no site gets more than one job's requests. The schedule and **Run now** only
  queue a job; a queued job asked for again runs once. The page shows it **Queued** until a
  runner takes it, and checks every two seconds while a job of yours waits or runs (thirty
  otherwise), not while the page is hidden (another tab, a minimized window): at once when it
  shows again. Everyone's load is shown, nobody's jobs: above the list a line such as *Runners 2/2
  · Browsers 3/3 (2 jobs, 1 recording) · 3 queued*, and beside a waiting job its place and why
  it waits: *2. in the queue: waits for a runner, all 2 busy (1 job ahead)*, *waits for its site
  (heise.de), used by another job* (its own site, never another job's), or, running, *Waits for
  a browser: all 3 in use (2 by jobs, 1 by recordings)* (`GET /api/status`, and in the log's
  polls).
- **Browsers:** up to `HARVESTER_BROWSERS` Chromiums at once (3 by default; each takes some
  300-600 MB): each running website job takes one, recordings the others, so people record side
  by side and while jobs run. When all are taken, the recorder's screen says so instead of staying
  empty, and how many jobs and recordings hold them (not whose). A browser is freed whenever its
  view ends: Done, Cancel, another view of the page, closing the page, the time limits, or
  (rclone's sign-in) the wizard's end.

## Proxy

- **Proxy:** with a proxy a job may go **Through the proxy**: its downloads, its browser, yt-dlp
  and gallery-dl use it, and sites that refuse servers' addresses see the proxy's. HTTP(S), with
  credentials in the address if it needs them (`http://user:password@host:3128`), or SOCKS5
  without a password (`socks5://host:1080`; `socks5h://` has the proxy look up the names), e.g.
  `ssh -D` or Tor. A SOCKS5 proxy with a password, or another kind, is refused (Chromium cannot
  send a SOCKS password): the app's log says why, once, and no proxy is used. The proxy's name
  changes what the form says, e.g. *Through the home connection* for a proxy at home. Without
  one the option is not offered, and a job that chose it goes directly. The recorder follows the
  form's choice.

## Bot checks: the browser, disguise and impersonation

- **A person's browser:** sites' bot protection (Cloudflare, ...) refuses a browser that says it is
  automated, or cuts its connection (`ERR_HTTP2_PROTOCOL_ERROR`). Chromium runs as the full browser
  in its new headless mode (not the headless shell, which the image does not have: an empty
  `HARVESTER_BROWSER_CHANNEL` is the full browser too), without the automation flag, with Chrome's
  user agent and one language and time zone for recordings and runs (`HARVESTER_LOCALE`,
  `HARVESTER_TIMEZONE`, default `en-US`, `Europe/Berlin`). A website job's **HTTP/1.1 only**
  serves the sites that cut HTTP/2 connections, in the recorder too.
- **Disguise the browser** (`browser/engine.py`; a website job's option, off by default): for sites
  whose bot check refuses the browser otherwise, the job's browser starts with
  [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python), a fork of Playwright
  patched where Playwright gives an automated browser away, instead of Playwright: the page sees
  none of Playwright's globals (`__playwright__binding__`, `__pwWebSocketDispatch`), its scripts run
  in isolated worlds, and Chromium starts without the automation's switches. Everything else is the
  same: the Chromium, the guard with the browser's credentials, the routes, the language, time zone
  and window. The job's runs, its **Test the recipe** and **Continue in the browser** take the job's
  flag (a challenge answered there is answered under the fingerprint the runs have, so Cloudflare's
  `cf_clearance` fits them), a new recording the form's. What it does not do: it answers no
  challenge (a person still does, in the browser), it does not change the address sites see (from a
  server's address Cloudflare may refuse it all the same: **Through the proxy** with a home
  connection helps most), and the job's own requests over HTTP stay Harvester's (**Impersonate a
  browser** has them present themselves as Chrome). It hides what a page reads of the automation,
  not the server: in tests from a server's address, bot.sannysoft.com found the same with and
  without it (only the WebGL renderer, a software one, failed), and Cloudflare's managed challenge
  on a test page stayed until a person answers it, either way. Patchright applies no WebSocket
  route, so a disguised page's WebSockets are checked by its guard alone: while the admin has the
  guard off, a disguised browser does not start (the job fails, saying so). In **Continue in the
  browser** and the recorder, recorder.js and its binding are in the page, disguised or not.
- **Impersonate a browser** (`sources/impersonate.py`; websites, feeds, releases, galleries): the
  job's own requests (a website's listing over HTTP and its files, also those of a recipe with the
  browser, a feed and its enclosures, release files, a gallery's images) present themselves as
  Chrome does: its TLS handshake, HTTP/2 and headers, by `curl_cffi` under httpx, for sites
  behind Cloudflare and similar checks that answer plain HTTP clients with 403. httpx keeps the
  rest: each redirect is a request of its own and checked as any (only public addresses), the
  cookies are its jar's, the job's headers go along (the app's user agent gives way to Chrome's),
  the answer comes in chunks under the job's largest file, and the proxy is the job's (http(s)
  or socks, as curl takes them). Off by default: only where a site refuses the downloads
  otherwise.

## Safety

- **Safety:** the browser's pages and the downloads reach only http(s) addresses outside local and
  private networks, checked on the addresses a name resolves to and on every request and redirect (a
  `file://` page would show the app's secret): a page's WebSockets too (one refused is never
  connected), its service workers are off, and a file a recorded site links to is downloaded by the
  app's guarded HTTP client with the browser's cookies, not by the browser. Behind those checks,
  every connection of a browser goes through its own guard, a proxy on 127.0.0.1 in a process of its
  own (`browser/guard.py`; for loopback too; without the app's secrets, undumpable, ending with the
  app; one that fails closes its port, and the browser's connections fail; each browser has
  credentials of its own for its guard, so one that reaches another browser's guard, on a crashed
  guard's port, say, is refused there), which sees what Playwright's routes do not, such as a web
  worker's WebSockets: it looks the name up (its checked addresses kept for a minute), refuses it
  unless every address is public, and connects to an address it checked, so a name that changes
  meanwhile (DNS rebinding) gains nothing; the home connection is asked for that address, not the
  name. With the admin's *Browser network guard* off, the browsers connect by themselves (the home
  connection given to Chromium): the routes still check what Playwright sees, but a web worker's
  WebSockets and a name that changes after its check (DNS rebinding) are then kept off the private
  network by the External Apps' firewall alone. WebRTC sends only through the proxy (no UDP of its
  own), and WebTransport does not connect through one. rclone's sign-in browser alone may open
  rclone's listener (`127.0.0.1:53682`), while rclone runs (without a guard, by the routes' check
  alone). yt-dlp, gallery-dl, rclone and mail servers connect by themselves (their addresses checked
  the same way before, not their redirects), as do the guarded HTTP client's downloads after their
  check: there the External Apps' network rules stay the guard. Pages and feeds are read up to 16
  MB, a file in chunks up to the job's largest size. Chromium, its Playwright driver, rclone,
  gallery-dl and ffmpeg run without the app's secrets in their environment (a proxy's password goes
  to gallery-dl in a 0600 file), and the app's process is undumpable, so its `/proc` files (its
  environment) are root's. The app runs as the user `harvester`, frpc as `harvester-frp`, whose
  configuration (HaRP's key) `harvester` may not read, both without a way back to root (`setpriv
  --no-new-privs`, `start.sh`). A job's id is never used again. A person's pattern runs with a time
  limit, in the recorder (2 s for a page) and in the runs (1 s a text: feeds' links, mail's senders,
  subjects and file names, release files, the wait pattern), letting the other threads go on
  meanwhile; one that takes longer fails its job with a message, not the app. The recorder's
  WebSocket takes only Nextcloud's origin (`NEXTCLOUD_URL`, more in `HARVESTER_ORIGINS`).

## Backup

Harvester keeps what is its own in one SQLite database, `harvester.db` in the app's persistent
storage (`APP_PERSISTENT_STORAGE`): the jobs, what each downloaded, their runs and progress log,
the site-changed summaries, the pictures of what a job's browser saw, the queued addresses and
the people's templates. The jobs' accounts and kept sessions are the people's sensitive
preferences in Nextcloud (encrypted), and come back with Nextcloud's own backup.

Copy the database while the app runs with SQLite's backup (a consistent copy, unlike copying
the file), for example inside the app's container:

```bash
docker exec -u harvester nc_app_harvester python3 -I -c 'import os, sqlite3
source = sqlite3.connect(os.path.join(os.environ["APP_PERSISTENT_STORAGE"], "harvester.db"))
copy = sqlite3.connect("/tmp/harvester-backup.sqlite3")
source.backup(copy)'
docker cp nc_app_harvester:/tmp/harvester-backup.sqlite3 .
```

To put a copy back, disable the app, replace `harvester.db` with it, and enable the app again.
