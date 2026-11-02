# Changelog

## Unreleased

### Fixed

- A browser is always freed: the end of rclone's wizard ends its sign-in (also from a worker
  thread), and a view of the page left without *Cancel* closes its connection.
- A remote's configuration set up anew for a job wins over the tokens kept from the one before;
  the kept configuration names its account.

### Security

- Every connection of a browser goes through its own guard, a proxy in the app: a web worker's
  WebSockets and anything else Playwright's routes do not see reach only public addresses, each
  name looked up once and its checked address connected (no DNS rebinding), also through the home
  connection. WebRTC sends nothing past the proxy. rclone's listener is open to the sign-in's
  browser alone.

## 0.8.1 - 2026-11-02

### Added

- **Changed tags reach the saved files:** a job remembers each file it saved and the tags it
  gave it. When the job's tags change, a pass right away adds the missing ones and takes off
  those the job gave and has no more; tags people added stay. Files saved before 0.8.1 are found
  by listing the site once, without downloads.
- **Sign in here** for a remote: the provider's sign-in for `rclone authorize` runs in the app's
  browser, besides pasting its token.
- Schedules every 15 and 30 minutes.

### Changed

- **Stop** ends a download in progress at once: its chunks, yt-dlp's, and gallery-dl's and
  rclone's (the helper program is killed). The file it was at comes next run.

## 0.8.0 - 2026-11-02

### Added

- **ffmpeg in the image:** video jobs join separate video and audio streams (mp4 where the
  codecs fit, else mkv). ffmpeg is started without the app's environment.
- **Set up with rclone** for a remote: a wizard that asks rclone's own configuration questions
  one at a time.
- **Every storage service of rclone 1.75** for remotes, except local, the wrappers, doi, hdfs,
  sia and oos; their server settings are checked, and a test compares them with rclone's own
  list.
- Templates suggest values for their parameters, offered as buttons.

### Fixed

- Options' suggestions are all shown, instead of a datalist that listed only the value already
  there.

### Security

- A remote takes no credentials from the app's machine (`env_auth`, `use_msi`,
  `use_emulator`): they are refused in a configuration and answered no by the wizard.

## 0.7.9 - 2026-11-02

### Added

- The address sites see is shown under the recorder's address bar and, for a run through the
  proxy, at its start (Cloudflare's trace, through the same connection).

## 0.7.8 - 2026-11-02

### Added

- **SOCKS5 proxies** (`socks5`, `socks5h`), without a password, which Chromium cannot send.

### Changed

- The proxy from the environment is checked as well, and a refused proxy is logged once.

## 0.7.7 - 2026-11-02

### Fixed

- The admin settings' selects show their options (now plain strings, as Nextcloud's select
  takes them).

## 0.7.6 - 2026-11-02

### Added

- The proxy and its name as admin settings: an http(s) address, with its credentials apart for
  Chromium. A job's option reads *Through \<name\>*.

## 0.7.5 - 2026-11-02

### Added

- Runners and browsers as admin settings (*Additional settings* > *Harvester*), read each
  minute; the environment's values are the defaults.

## 0.7.4 - 2026-11-02

### Added

- **Several runners** (`HARVESTER_RUNNERS`, 2 by default): jobs run side by side, never twice
  and one per site, with pause, resume and stop per running job.
- **A proxy** (`HARVESTER_PROXY`): a job may go through the home connection, for its downloads,
  its browser, yt-dlp and gallery-dl.

## 0.7.3 - 2026-11-02

### Added

- **Several browsers** (`HARVESTER_BROWSERS`, 3 by default): people record side by side and
  while a job runs. When all are taken, the recorder's screen says so instead of staying empty.

### Changed

- A stop also ends a wait (a retry, the site asking to wait) at once.
- The recorder's screen shows the page's cursor (a crosshair only while picking), and the wheel
  scrolls smoothly.

## 0.7.2 - 2026-11-01

### Added

- **Pause, resume and stop a running job:** while it runs, *Run now* becomes *Pause* and
  *Stop*. A stopped run keeps what it saved, and its next run goes on from there.

### Fixed

- The recorder's first page is shown once: two screencasts stopped each other and broke the
  screen.
- A browser that does not start is told on the page instead of an empty screen.

## 0.7.1 - 2026-11-01

### Fixed

- The progress log fills: its first poll ran before the panel was shown.
- Outcome and error colours are readable.
- *Full window* uses the browser's full screen, with a drawn icon.

## 0.7.0 - 2026-11-01

### Added

- **A progress log** below the jobs, of all jobs or one: what the runs do (searching, each file
  downloaded, saved or found already there, errors, the outcome), live while one runs.
- **A person's browser** for sites' bot protection: Chromium runs as the full browser in its new
  headless mode, without the automation flag, with Chrome's user agent and one language and time
  zone (`HARVESTER_LOCALE`, `HARVESTER_TIMEZONE`). A website job's **HTTP/1.1 only** serves the
  sites that cut HTTP/2 connections, in the recorder too.
- *Full window* for the recorder: the browser takes the whole window, with the controls floating
  over the screen and the recipe in a drawer.

### Changed

- A new run clears its job's last error.

### Fixed

- Every file over 5 MB failed with 400 on storage with multipart uploads (S3): files go to
  Nextcloud in nc_py_api's first kind of chunked upload, whose first chunk Nextcloud accepts.

## 0.6.3 - 2026-11-01

### Fixed

- Every newly downloaded file failed the run with "[404] Not Found": the file uploaded was
  tagged as file 0. It is now looked up for its ID, so new files get their tags.
- The templates' address check refused every address with an "s" after the scheme, which is
  almost every https:// feed, page or repository.

## 0.6.2 - 2026-11-01

### Fixed

- A file not yet in Nextcloud no longer stops the job: nc_py_api 0.30.3 reports a missing path
  as a plain 404, which is now taken as missing too. Other errors still stop the run.

## 0.6.1 - 2026-11-01

### Fixed

- Since 0.5.0 a website job in the browser failed at its first file with "Cannot run the event
  loop while another loop is running". Nextcloud's requests now run on an event loop in a thread
  of their own.

### Changed

- The README is up to date and wrapped at 100 characters, kept so by a test.

## 0.6.0 - 2026-11-01

### Added

- **One-time codes** (TOTP, from a base32 secret or an otpauth:// link): the recorder marks a
  code field, and the replay types the current code.
- **Cookies** from a browser's cookies.txt export or Cookie header pairs, for websites, feeds,
  yt-dlp and gallery-dl.
- HTTP Basic for feeds and websites without steps, sent to the site's own host and scheme only.
- OAuth2 for mail (Google, Microsoft, any token endpoint) with XOAUTH2, keeping a replaced
  refresh token.
- The page shows each credential's title and help, hides secrets, and takes cookies in a text
  area.

## 0.5.0 - 2026-11-01

### Added

- **Templates for common uses:** a podcast, a video channel, a channel as audio, invoices from
  email, an app's releases, the PDFs a page links to, a folder of another Nextcloud (public
  link) and a gallery, besides heise's magazines. Each card shows its source's icon.
- **Releases from GitLab** (nested groups, asset links, `PRIVATE-TOKEN` sent to the GitLab host
  only) **and Codeberg**. A repository on another server is recognised by asking its API.
- Forms with the right controls: checkboxes for on/off settings, lists for file types, addresses
  and patterns checked as typed, numbers with bounds, suggestions (yt-dlp formats, IMAP
  folders), sizes in readable units, the schedule in words with its next runs, and -1 files per
  run for no limit.
- A largest file per job (4 GB), checked before and while downloading, the browser's downloads
  too.

### Changed

- nc_py_api's async client (nc_py_api 0.31 removes the sync one).
- A run ends after two hours and goes on in the next; a repeated failure is notified once.
- Downloads retry dropped connections; yt-dlp has timeouts and retries.
- The app's SQLite database runs in WAL mode with a busy timeout.

### Security

- Chromium, rclone and gallery-dl get an environment without the app's AppAPI secret and HaRP's
  key.
- Every HTTP request, redirects and the browser's requests included, goes only to public
  addresses, with host names resolved.
- The recorder's WebSocket accepts only Nextcloud's own pages (Origin).
- The app and frpc run as the user `harvester` with `no_new_privs`; the API's input is bounded.

### Fixed

- The executor's loop survives any error.

## 0.4.2 - 2026-10-31

### Fixed

- The recorder types with the person's own keyboard layout (AltGr, Option, dead keys, input
  methods): "@" on a German keyboard became a Ctrl+Alt shortcut. Keys that do not type and
  shortcuts go as key presses; long pastes are inserted at once.
- A tag already on a file, or one whose name a tag the user cannot see has (409 Conflict), no
  longer fails the job.
- A file the site will not give no longer blocks the job; it is tried again next run.
- A scheduled job is read again just before its run, and a job deleted while it runs ends
  quietly.
- Gallery: the files of a post of several are told apart; a listing with a partial failure is
  used.
- Media: an entry that cannot be had (private, removed) is skipped.
- Feeds: relative links are taken against the feed's address.
- Mail: accounts with non-ASCII or spaced names sign in (SASL PLAIN); folder names are sent in
  IMAP's modified UTF-7.
- A website's start page without any of the recipe's links fails ("signed out, or the site
  changed?") instead of ending as a success.
- Notifications show `{`, `}` and `%` as text; tags are cut to Nextcloud's 64 characters; path
  parts lose control characters, backslashes and trailing dots; an empty file name is an error.
- A changed source or recipe forgets the complete-pass mark; counts must be at least 1.
- The recorder's WebSocket survives errors without text and broken recipes; rclone's messages
  go to a file; a name with a dot of its own still gets the file type's extension.

### Security

- Remote: an indented rclone line or a value of several lines is refused, since a command (such
  as `bearer_token_command`) could hide in a value and run in the container. `token_url` and
  `storage_url` are checked as servers too.

## 0.4.1 - 2026-10-31

### Fixed

- The first download of every job failed with "[404] Not Found": a file missing in Nextcloud is
  now one to download, not an error.

## 0.4.0 - 2026-10-31

### Added

- **Six more sources:** mail attachments over IMAP (TLS, read-only, filtered by sender,
  subject, file name, type and size); RSS and Atom feeds (enclosures such as podcasts, or entry
  links); videos and audio through yt-dlp (single-file formats); images listed by gallery-dl;
  file stores and cloud drives through rclone (WebDAV, SFTP, FTP, S3, public shares, Drive,
  Dropbox, OneDrive); and release assets from GitHub, Forgejo or Gitea.
- **Sessions:** a job keeps its browser session and replays the sign-in only when the listing
  shows a login form.
- **Challenges** (reCAPTCHA, hCaptcha, Cloudflare): the run stops, the job shows *Needs you*,
  and its user is notified. *Continue in the browser* opens the job's session at that page for
  the person to answer; Harvester never answers a challenge.
- The New job page offers every source, each with a description; tokens and configurations are
  asked for hidden.

### Changed

- Every source that downloads over HTTP shares the download's checks (the site's pauses, no web
  page instead of the file, the type and size).

### Fixed

- A sign-in step that failed left the browser busy for every later job and recording.

## 0.3.2 - 2026-10-31

### Fixed

- The page could not be clicked: it was squeezed behind AppAPI's placeholder. It now takes the
  placeholder's place and looks like Nextcloud: a header with the action, a jobs table with
  status and tags, and cards for the choices.

## 0.3.1 - 2026-10-31

### Changed

- The image is published on ghcr.io (`ghcr.io/sarumaj/harvester`), which `info.xml` names, and
  on Forgejo's registry; a deploy daemon's registry mapping takes it from there.
- A tag's release carries the source archive the image is built from.

## 0.3.0 - 2026-10-31

### Added

- **Harvester**, a Nextcloud External App (AppAPI, Python): jobs fetch files from websites into
  people's folders on a schedule and tag them.
- **Recipes instead of code:** sign-in steps (replayed in Chromium), the listing, levels of
  links to follow, the file links and the next page. Patterns with named groups make the
  download URL, the file names and the tags.
- **The recorder**, a Chromium driven from the app's page: it records the sign-in (never the
  password), proposes a pattern for each pick, outlines what it matches and shows examples with
  their values.
- heise's magazines as a template with parameters.
