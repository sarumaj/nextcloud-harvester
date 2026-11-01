# Harvester

A Nextcloud External App (AppAPI, Python) that fetches files into your folders on a schedule
and tags them: from websites, mailboxes, feeds, video and gallery sites, file stores and cloud
drives, and release pages. It is generic: a website is described by a **recipe**, recorded in
the app's browser or taken from a template, not by code.

- **Jobs:** a source and its settings, a folder, a cron schedule (UTC), tags and an account.
  *Harvester* in Nextcloud's top menu lists your jobs, runs one now and edits them. The form
  explains the schedule in words as it is typed, with its next runs, and shows sizes in KB, MB
  or GB. Below the jobs, a **progress log** of what the runs do (searching, each file downloaded,
  saved or found already there, errors, the outcome), live while one runs, of all jobs or of one;
  a new run clears its job's last message.
- **Sources** (`ex_app/lib/harvester/sources/`), one card each on the *New job* page:
  - `site`: files linked from a website's pages, by a recipe (below).
  - `mail`: attachments of the messages in an IMAP mailbox (TLS), filtered by sender, subject,
    file name, type and size; read-only (`BODY.PEEK`, the folder selected read-only), so nothing
    is marked as read. Values: `{from}`, `{sender}`, `{subject}`, `{date}`, `{year}`, …
  - `feed`: the files of an RSS or Atom feed, newest first: its enclosures (podcast episodes) or
    each entry's link, through the checked download. Values: `{title}`, `{feed}`, `{author}`,
    `{date}`, `{year}`, `{filename}`, …
  - `media`: videos and audio from YouTube, Vimeo and the other sites yt-dlp supports (a video,
    or the newest entries of a playlist or channel); formats that come as one file only, as the
    image has no ffmpeg to join streams. Live and upcoming streams are skipped.
  - `gallery`: images of galleries, albums and profiles on the sites gallery-dl supports;
    gallery-dl lists them, Harvester downloads them with its checks and the page as `Referer`.
  - `remote`: the files in a folder of a WebDAV, SFTP, FTP or S3 server, a Nextcloud public
    share, Google Drive, Dropbox, OneDrive and other rclone remotes. The job's account is an
    rclone configuration with one remote (OAuth tokens from `rclone authorize` on a computer).
    Only remotes of other machines: no local files, wrappers, commands or private hosts.
    Refreshed tokens are kept as the job's session.
  - `releases`: the files attached to a repository's releases on GitHub, GitLab (gitlab.com or
    a server of its own, projects in groups of groups), Codeberg or another Forgejo or Gitea
    server, newest first (prereleases if asked, drafts never), filtered by a pattern on the
    file's name; values `tag`, `version`, `name`, `repo`, `year`. A token (the job's account)
    for private repositories and GitHub's request limit.
- **Recipes** (`ex_app/lib/harvester/recipe.py`): the steps to sign in (replayed in Chromium;
  without steps the pages are fetched over HTTP), the listing to start at, levels of links to
  follow (a year, then its issues), the file links, and the next page's link. Each level has
  a CSS selector and a pattern: a regular expression on the link's URL whose named groups
  (`(?P<year>\d{4})`) become values.
- **Values:** the download URL (`{url}/download`), the file's name
  (`{year}/iX {year}-{issue:02d}`) and the tags (`{year}`) are templates of the link's values:
  the pattern's groups and `url`, `text`, `page`, `host`, `filename`, `stem`, `ext`.
- **Recorder** (`ex_app/lib/harvester/browser/`): a headless Chromium in the app, shown on the
  page and driven from it over a WebSocket. Sign in and go to the listing (recorded as steps),
  browse without recording, then pick the links to follow and the file links: the app proposes
  a pattern with named groups for each (`patterns.propose`), outlines what it matches in the
  page (solid) and what it leaves out (dashed), lists examples with their values, and proposes
  a file name and tags. Edit a pattern and the outlines follow. Passwords are never recorded:
  the job fills in its account. **⛶ Full window** gives the browser the whole window (it takes
  the window's size, pages lay out for it), with the controls floating over the screen and the
  recipe in a drawer.
- **Sessions and challenges:** a job keeps its browser session (cookies, local storage;
  encrypted in Nextcloud, like its account) and replays the sign-in only when the listing shows
  a login form. When a site asks for a person instead (reCAPTCHA, hCaptcha, Cloudflare, "verify
  you are human") the run stops, the job shows **Needs you** and its user gets a notification;
  scheduled runs wait. **Continue in the browser** opens the recorder's browser with the job's
  session at that page: the person answers it, **Done** keeps the session and runs the job.
  Harvester never answers a challenge itself.
- **A person's browser:** sites' bot protection (Cloudflare, ...) refuses a browser that says it is
  automated, or cuts its connection (`ERR_HTTP2_PROTOCOL_ERROR`). Chromium runs as the full browser
  in its new headless mode (not the headless shell), without the automation flag, with Chrome's
  user agent and one language and time zone for recordings and runs (`HARVESTER_LOCALE`,
  `HARVESTER_TIMEZONE`, default `en-US`, `Europe/Berlin`). A website job's **HTTP/1.1 only**
  serves the sites that cut HTTP/2 connections, in the recorder too.
- **Downloads:** a pause between files; waits when the site asks (429 or 503 with
  `Retry-After`, or a wait pattern on the URL it redirects to, like heise's `wait_sec=`) and
  three retries of a broken transfer; never a web page instead of a file; expected file types
  (`pdf`, `zip`, `image`, ..., several separated by commas), a smallest and a largest size.
  Files go to Nextcloud in chunks of nc_py_api's first kind: its second numbers the first chunk 0,
  which Nextcloud refuses where the storage takes multipart uploads (S3), so every file over 5 MB
  failed with 400.
- **Repeat runs:** a job remembers what it downloaded; a file already in the folder is tagged
  and not downloaded again. At most `max_items` files per run (`-1`: no limit), the rest comes
  next time; a run ends after two hours in any case. One file that fails does not stop the
  others (it is tried again next run), five failures without a success do. Once a run went
  through the whole site, a listing of known files ends the next runs.
- **Notifications:** a job that needs a person, or fails, notifies its user; the same failure
  again on the next runs does not.
- **Templates** (`ex_app/lib/harvester/presets/*.json`), jobs with a few parameters, one card
  each on the *New job* page:
  - `heise`: heise's magazines (iX, c't, ...) as PDF for a subscription;
  - `pdfs-on-a-page`: the PDFs linked from a page;
  - `mail-invoices`: invoices (PDF attachments) from a sender;
  - `podcast`: a podcast's episodes;
  - `video-channel` and `channel-audio`: a channel's newest videos, or their audio;
  - `gallery`: a gallery or album's images;
  - `nextcloud-share`: the folder of another Nextcloud's public link;
  - `app-releases`: an application's release downloads.
- **Accounts:** a job's username and password, token or rclone configuration are your sensitive
  preferences in Nextcloud (encrypted); the page only shows whether they are set. Besides a
  password, a job signs in with:
  - **one-time codes** (`otp.py`, TOTP): a recorded sign-in's code field gets the current code
    from the secret an authenticator app would be given (base32 text or the `otpauth://` link);
  - **cookies** (`cookies.py`) of a browser you signed in with: a cookies.txt export or a
    Cookie header's `name=value` pairs, for websites, feeds, yt-dlp and gallery-dl (as a 0600
    file, removed after the run, never on a command line);
  - **HTTP Basic** for feeds and websites without recorded steps, sent only to the site's own
    host and scheme;
  - **OAuth2** for mail (Gmail, Outlook or any provider's token endpoint): a client ID and secret
    and a refresh token sign in with XOAUTH2; a refresh token the provider replaces is kept.
- **Safety:** the browser and the downloads reach only http(s) addresses outside local and
  private networks, checked on the addresses a name resolves to and on every redirect (a
  `file://` page would show the app's secret). Chromium, rclone and gallery-dl run without the
  app's secrets in their environment. The app runs as the user `harvester` without a way back
  to root (`setpriv --no-new-privs`, `start.sh`). The recorder's WebSocket takes only Nextcloud's
  origin (`NEXTCLOUD_URL`, more in `HARVESTER_ORIGINS`).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/playwright install chromium   # for the browser's tests (the full browser)
.venv/bin/python -m pytest -q
.venv/bin/black --check ex_app tests && .venv/bin/pylint ex_app/lib tests
```

The tests run against a fake magazine archive (`tests/fake_site.py`), over HTTP and in
Chromium, without network.

The image (`Dockerfile`, from Nextcloud's app-skeleton-python, with Chromium) is built and
registered by the homelab's `nextcloud` role (`nextcloud_harvester`), as
`local.homelab/harvester:<version>`. `appinfo/info.xml` describes the app; its routes are also
in the role's registration.

## Releases

A tag `vX.Y.Z` (the version and the image tag in `appinfo/info.xml`) runs
`.forgejo/workflows/release.yml`:

- **Archive:** `harvester-X.Y.Z.tar.gz`, the source the image is built from, on the Forgejo
  release (signed with `APP_SIGNING_KEY` when set).
- **Image:** built with Kaniko (the runners' job containers have no Docker socket and no user
  namespaces, so neither Docker nor Buildah works there) and pushed as `X.Y.Z` and `latest`, the
  same build and digest, to every registry that has a token:
  - GitHub's `ghcr.io/<GHCR_OWNER>/harvester`, the one `info.xml` names (registry
    `ghcr.io/sarumaj`, image `harvester`): secret `GHCR_TOKEN`, a classic GitHub token with
    `write:packages`; variables `GHCR_USER` (its account) and `GHCR_OWNER`. A new package on
    ghcr.io is private: make it public once, under the package's settings.
  - Forgejo's `git.ciepiela.org/dawid/harvester`: secret `PACKAGES_TOKEN`, the owner's token with
    `write:package` (the job's own token may not push packages).

  Without a token the image is still built (the build is checked), and not pushed.

**Taking the image from Forgejo instead of ghcr.io:** AppAPI pulls from the one registry in
`info.xml` and has no fallback of its own, but a deploy daemon can map a registry to another,
replacing only that part of the image's name. That is why the registry part holds the owner:

```bash
occ app_api:daemon:registry:add <daemon> --registry-from=ghcr.io/sarumaj --registry-to=git.ciepiela.org/dawid
```

The homelab registers the app with its own local image (`local.homelab/harvester`) and needs
neither.

For the app store, the store's certificate for `harvester` then signs the archive, and the
release can be published there as the other apps are.

## Licence

AGPL-3.0-or-later, see `LICENSE`.
