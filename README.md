# Harvester

A Nextcloud External App (AppAPI, Python) that fetches files into your folders on a schedule
and tags them: from websites, mailboxes, feeds, video and gallery sites, file stores and cloud
drives, and release pages. It is generic: a website is described by a **recipe**, recorded in
the app's browser or taken from a template, not by code.

- **Jobs:** a source and its settings, a folder, a cron schedule in your time zone (a new job
  takes the browser's, an edited one keeps its own; `0 6 * * *` is 06:00 there in summer and
  winter; jobs from before time zones keep UTC), tags and an account.
  *Harvester* in Nextcloud's top menu lists your jobs, runs one now and edits them; a job asked
  to run waits as **Queued** for its site or a free runner, a disabled one is marked *off*, and
  a button the app refuses (Stop as the run ends) says why beside the job. The form explains the
  schedule in words as it is typed, with its next runs, and shows sizes in KB, MB or GB of 1000,
  as the progress log and the size limits count them. The account's fields are part of the form,
  a new job's too: *Save* stores the job and what was typed into them (an empty field keeps what
  is stored), and *Save account* stores the account alone. Fields with usual values (the
  schedule, file names, formats, patterns, sizes, files per run) offer them all as buttons below
  the field, which takes other values too; options with a fixed set of values are lists. Below
  the jobs, a **progress log** of what the runs do (searching, each file downloaded, saved or
  found already there, errors, the outcome), live while one runs, of all jobs or of one; a new
  run clears its job's last message.
- **On-demand jobs** (`ex_app/lib/harvester/ondemand.py`): a job of a source with an address
  (`site`: its recipe's listing; `feed`, `media`, `gallery`: their page or feed) may be **On
  demand**: no schedule (it is never due; the list says *On demand*), and it runs when you send it
  an address.
  *Fetch a URL…* beside the job asks for one (`POST /api/jobs/<id>/fetch` with `{"url": ...}`);
  the run fetches that address in place of the job's own, for that run alone, with everything
  else the job's (folder, file names, tags, account, files per run, the home connection). The
  job's own address may stay empty (each fetch brings one; *Run now* then is not offered). The
  address is checked first as the source checks its own (http(s), not a local or private host);
  a job with an account and an address of its own fetches only that site's addresses (the same
  host, with or without `www.`, or a subdomain), since its user name and password are for that
  site. `mail`, `remote` and `releases` have no address a page could send: no on-demand jobs.
  The addresses wait in the database (a restart loses none), one after another per job, each
  once (sent again while it waits, it keeps its place); a job's next address goes back to the
  queue's end, behind other jobs. The job's row lists them, the one being fetched (*Stop* ends
  it) and the others with *Cancel* (`DELETE /api/jobs/<id>/fetch/<fetch id>`); its waiting reason
  counts them. The log names each address (*Fetching https://...*, and the outcome after it); the
  files a job fetched before are not fetched again, from any address, and the log says how many
  were skipped so. A job put back on a schedule drops the addresses still waiting.
  **One-time:** an on-demand job may be deleted after a run that went through (once the
  addresses waiting for it are fetched; not after a stop), with its account and session; its
  files stay, and a notification says what it did. One that failed is kept, to show why.
  **Send to Harvester:** below the jobs, a bookmarklet to drag to the bookmarks bar opens
  Harvester's page (`/index.php/apps/app_api/embedded/harvester/harvester?fetch=<the page>`),
  which asks which on-demand job fetches the page, or makes a new one from it: one button per
  source with an address, its form filled with the page as the job's address, *On demand* on;
  saved, it fetches the page at once. The page takes `fetch` out of its own address, so a reload
  shows the jobs.
- **Sources** (`ex_app/lib/harvester/sources/`), one card each on the *New job* page:
  - `site`: files linked from a website's pages, by a recipe (below).
  - `mail`: attachments of the messages in an IMAP mailbox (TLS), filtered by sender, subject,
    file name, type and size; read-only (`BODY.PEEK`, the folder selected read-only), so nothing
    is marked as read. Values: `{from}`, `{sender}`, `{subject}`, `{date}`, `{year}`, …
    A run asks little of the server: a sender that is a plain address or domain is searched for
    there, the headers come a hundred messages at a time, a message larger than the largest file
    is skipped unread, and a message whose attachments were all saved or skipped (or that is not
    the job's) is not read again until the job's server, folder, filters, types or sizes change.
    Only the attachment the run is at is held in memory.
  - `feed`: the files of an RSS or Atom feed, newest first: its enclosures (podcast episodes) or
    each entry's link, through the checked download. Values: `{title}`, `{feed}`, `{author}`,
    `{date}`, `{year}`, `{filename}`, …
  - `media`: videos and audio from YouTube, Vimeo and the other sites yt-dlp supports (a video,
    or the newest entries of a playlist or channel). A format of separate video and audio streams
    (YouTube's best, from 720p up) is joined with ffmpeg, without re-encoding: into mp4 where the
    codecs fit (H.264, AV1 with AAC), else mkv (VP9 with Opus). ffmpeg runs without the app's
    environment, as the browser does. Live and upcoming streams are skipped; an entry that
    cannot be had (private, removed) is skipped with a line in the progress log, and tried again
    next run. A video larger than the job's largest file is not downloaded (yt-dlp's
    `max_filesize`).
  - `gallery`: images of galleries, albums and profiles on the sites gallery-dl supports;
    gallery-dl lists them, Harvester downloads them with its checks and the page as `Referer`.
  - `remote`: the files in a folder of a WebDAV, SFTP, FTP or S3 server, a Nextcloud public
    share, Google Drive, Dropbox, OneDrive and other rclone remotes. The job's account is an
    rclone configuration with one remote (OAuth tokens from `rclone authorize` on a computer).
    Only remotes of other machines: every one of rclone's storage services (54 in rclone 1.75,
    from S3, B2 and Azure to pCloud, Mega, Proton Drive, iCloud Drive, Jottacloud, SMB, Storj
    ...), not local files, the wrappers (alias, crypt, union, ...), DOI, HDFS, Sia or Oracle
    Object Storage, whose files, servers or sign-ins the app cannot check. No commands, no private
    hosts, no credentials from the app's machine (`env_auth`, `use_msi`, ...). A new rclone's
    remotes are refused until they are looked at (`scripts/rclone_backends.py`, then the lists in
    `sources/remote.py`; a test fails until each is in one).
    Refreshed tokens are kept as the job's session, for that account only: one set up anew wins.
    **Set up with rclone**, beside the account's field, asks rclone's own configuration
    questions one at a time (`rclone config` without a terminal, in a throwaway configuration):
    the kinds of remote allowed, no advanced settings,
    files or commands. Where a provider signs in with a browser, there are two ways: **Sign in
    here** runs `rclone authorize` in the app and shows the provider's sign-in in the app's
    browser (as the recorder does), which may open rclone's listener `http://127.0.0.1:53682/`
    and no other local address while it runs; rclone's token answers the question. One sign-in
    at a time in the app (rclone's port is fixed), ten minutes at most, never through the proxy.
    Some providers refuse such a browser (Google: "This browser or app may not be secure"); then
    run the `rclone authorize "<type>" ...` command it shows on your computer and paste what it
    prints. The finished remote, checked as a run checks it, goes into the field to be reviewed
    and saved with the job (or *Save account*).
  - `releases`: the files attached to a repository's releases on GitHub, GitLab (gitlab.com or
    a server of its own, projects in groups of groups), Codeberg or another Forgejo or Gitea
    server, newest first (prereleases if asked, drafts never), filtered by a pattern on the
    file's name; values `tag`, `version`, `name`, `repo`, `year`. A token (the job's account)
    for private repositories and GitHub's request limit. **Newest releases** takes the files of
    that many releases only (`0`, the default: every release on the pages read, as before; the
    `app-releases` template: 1), so a new job does not fetch a whole release history.
- **Recipes** (`ex_app/lib/harvester/recipe.py`): the steps to sign in (replayed in Chromium;
  without steps the pages are fetched over HTTP), the listing to start at, levels of links to
  follow (a year, then its issues), the file links, and the next page's link. Each level has
  a CSS selector and a pattern: a regular expression on the link's URL whose named groups
  (`(?P<year>\d{4})`) become values.
- **Numbered pages:** a listing paged by numbers (`‹ 1 [2] 3 … 9 ›`) instead of one "next" link
  has `"next": {"kind": "number", "selector": <the numbers' container>, "level": 0}`
  (`harvester/pagination.py`). On each page the walk takes the current page's number (the one
  marked with `aria-current` or a class like `active`, `current` or `selected`; else the one
  number that is no link; else `?page=`, `/page/3` in the address; else its own count) and
  follows the number one higher: a link, or a button it clicks (`"buttons": true`, which needs
  the browser). It ends at the last number, after *Pages*, or at known files, as with a link.
  A next without `kind` is the single link it always was.
- **Scroll to load more:** the step `{"action": "scroll"}` (`"rounds"`: at most 1 to 100, 20 by
  default) scrolls the page to its end until it stops growing (no new links, elements or
  height two rounds in a row, or after a minute), for a listing that loads more as it is
  scrolled. The listing the steps end on is read in their tab, as they left it; with a kept
  session the sign-in is skipped but the scroll steps at the steps' end still run. Ordinary
  scrolling is never recorded: a position depends on the window's size and the page's state,
  and replays wrongly in a job's window; a **Load more** button is a click, recorded as one.
- **Steps in frames:** recorder.js runs in every frame of the page, cross-origin ones (a consent
  banner's, a sign-in's) included. A step done inside a frame keeps the frame's path from the
  page down, `"frame": [{"selector": "iframe[title=\"Consent\"]", "url":
  "https://consent.example/cmp", "name": "…"}, …]`: each frame's element in its parent, its
  address without query and fragment, and its name. A replay finds each frame by its selector,
  else by its address (a query that changed since the recording does not matter), else by its
  name, waiting for the page to build it. The recorder says where the mouse or the focus is
  (**Acting in: the page**, or **Acting in a frame: consent.example (1st level)**), and the
  Steps list marks a frame's steps (`⧉ in frame consent.example`), as do the log's step labels
  (`step 2 (click “Akzeptieren” in frame consent.example) failed`). Picks (links to follow, file
  links, the next page) are the page's own: one inside a frame is refused with the frame's
  address, to open and pick on directly. A step without `frame` acts on the page, as before.
- **Optional steps:** a step with `"optional": true` is best effort, for what shows only
  sometimes (a consent banner on the first visit, a notice). It waits 5 seconds at most for its
  frame and element; when it fails (not there, not visible, detached) the run goes on, logs
  `step 2 (click “Akzeptieren”, optional) skipped: <why>`, keeps no picture and counts no
  failure. A stop is never taken for such a failure. Each step in the recorder's Steps list has
  its switch **optional**; a click whose text reads like a consent button (*Accept*, *OK*,
  *Alle akzeptieren*, *Zgadzam się*, …) gets the hint **make optional?**, never more.
  **Test the recipe** lists the steps it went on without apart from the problems.
- **What the recorder records:** clicks, fills (one per field: a field Enter submitted is not
  filled again, and the click on the submit button the browser makes for Enter is none), a
  list's choice (`{"action": "select", "value": …, "label": …}`, replayed by its value, else by
  the text it shows), a checkbox's label as one click (not also the click the browser passes on
  to the box), text edited in place (`contenteditable`, a fill), and the keys that work a list of
  suggestions in a combobox or autocomplete (↑, ↓, Escape, Tab, Enter: steps "press"); Enter in a
  field is a step too, other keys are what they type. A file to upload is refused with a message
  (a job has none). A click on an element that showed only under the mouse (a menu opened by
  `:hover` or by a script) comes after a step `{"action": "hover"}` over the menu. Elements in
  open shadow roots are recorded with a selector through their host (`#box #user`, which
  Playwright follows into the root); a click in a closed one is recorded on its host, and the
  recorder says so. A password is never recorded: a field that was ever of type password, or has
  `autocomplete="current-password"` or `new-password`, stays one when a "show password" switch
  makes it text, and its step fills in the account's. A one-time code is recognised by its name
  or `autocomplete="one-time-code"`, and after a password also as a field named `code`, `otc`
  (Microsoft's) or `challenge` (Nextcloud's), or one for 6 to 8 digits.
- **Popups:** a tab the page opens while recording (a "Sign in with …" window) is shown, and is a
  step `{"action": "popup"}`: the steps after it act in that tab. When it closes itself, the page
  that opened it is shown again, a step `{"action": "opener"}`. A job waits for the popup (the
  step's time) and acts in it, and goes back once it closed.
- **Waits:** a click or a press after which the page went to another address keeps it as a
  pattern, numbers and tokens left open and any query
  (`"wait": {"url": "^https://shop\\.example/orders/\\d+(?:[?#].*)?$", "selector": "#next"}`),
  and the next step's element. The replay waits for both (the step's time), then a short while
  for the network, instead of up to 15 seconds for a page that never goes quiet. A step without
  `wait` waits for the network as before.
- **Values:** the download URL (`{url}/download`), the file's name
  (`{year}/iX {year}-{issue:02d}`) and the tags (`{year}`) are templates of the link's values:
  the pattern's groups and `url`, `text`, `page`, `host`, `filename`, `stem`, `ext`.
- **Recorder** (`ex_app/lib/harvester/browser/`): a headless Chromium in the app, shown on the
  page and driven from it over a WebSocket. Sign in and go to the listing (recorded as steps),
  browse without recording, then pick the links to follow and the file links: the app proposes
  a pattern with named groups for each (`patterns.propose`), outlines what it matches in the
  page (solid) and what it leaves out (dashed), lists examples with their values, and proposes
  a file name and tags. Edit a pattern and the outlines follow. Passwords are never recorded:
  the job fills in its account. The **Steps** list edits the steps: **↑** and **↓** move one,
  **✕** removes one, a fill fills in a value of its own (edited there), the account's user name,
  password or the current one-time code, a list's choice can be edited, and a step's wait dropped
  (**wait for the network instead**). A recipe with steps opens in **Browse**; **Record steps**
  then asks where the new ones go: after a step (the last, unless chosen), keeping the later ones
  (**Append**), instead of them (**Replace from there**), or instead of all (**Replace all**).
  A fill with a value of its own in what looks like a password's field (`#pw`, `[type=password]`,
  a name with `pass` or `pwd`), from an earlier recording, is pointed out in the job's form, in
  the recorder and in the app's log when the job is saved, by its step and field, never its
  value. **Back**, **Forward**, **Reload** and the address field's **Go** are buttons (Tab and
  Enter reach them), here and in **Continue in the browser**. They
  are steps only while recording: an address typed, Back and Forward become a step "goto" to the
  page arrived at (a job's browser has no history to go back in), Reload none; browsing and
  picking record nothing. Every pick (each level of links to follow, the file links, the next
  page) has the same controls: its element (CSS selector) and pattern to edit, **Pick again**
  and **Remove** (a removed level's later levels move up); **Undo pick** takes back the last
  change of the picks. The recorder starts with the form's recipe, so its picks can be changed
  there. Picking a number of a numbered pagination as the next page proposes numbered pages,
  showing this page's number and the next one's (**Use the picked link alone** switches back).
  **Add: scroll to load more** (while recording) adds that step and scrolls the page.
  **Test the recipe** runs it as a job would (the site source's own walk, `Site.survey`, in a
  browser context of its own with the recorder's session, shown on the screen; nothing recorded,
  nothing downloaded) and lists the pages it read with the links matched, the first 20 file
  links with the names the job's *File name* gives them, and the problems: a step that failed
  (with the page it was on), a selector that matched nothing, a pattern none matched. It reads
  at most 30 listing pages and three minutes; **Stop the test** ends it, and its context closes.
  A recipe without steps is fetched over HTTP by the job: the test says so. **⛶ Full window** gives
  the browser the whole screen (the browser's full screen, above Nextcloud's header; Esc leaves it):
  it takes the screen's size, pages lay out for it, the controls float over it and the recipe is in
  a drawer. When the browser cannot start, the page says why instead of showing an empty screen. The
  screen takes the cursor the page shows under the mouse (a crosshair while picking); the wheel's
  movement goes to the browser once a frame, in pixels.
- **Cookies** (`browser/jar.py`): both browser views (the recorder, **Continue in the browser**)
  have a **Cookies** panel with the browser's cookies, grouped by domain: this site's (those the
  page gets) or all, with path, expiry ("session" without one), Secure, HttpOnly and SameSite.
  Values stay hidden (`•••• (8)`, their length) until **Show**: they are often the sign-in
  itself, and the WebSocket carries one only when asked to show or export it; none is logged.
  **Edit** changes a cookie's value (left empty: unchanged), name, path, expiry or flags,
  **Delete** removes one, **Delete all** a domain's, **Add** one (the page's host, `/`, Secure).
  A change is the browser's at once. The app checks each: a name and value without control
  characters (nor `;`, `,`; a name without `=` or spaces), name and value 4 kB at most, a path
  from `/`, an expiry at most 400 days ahead, SameSite None only with Secure, and a domain of the
  sites the browser is at or has cookies of (one of them, a parent that is no public suffix, or a
  subdomain): no cookies for other sites. The public suffixes (`com`, `co.uk`, `github.io`) are
  the list of the `publicsuffixlist` package, pinned in `requirements.txt` and never fetched.
  **Into the account** writes the shown cookies as a
  cookies.txt into the job's *Cookies* account field: in the recorder the form's field, stored
  with the job's Save; in the job's browser after the person confirms (a new account forgets
  the kept session). **Use for this job** (the job's browser) keeps the browser's session for
  the job's next run, as Done does, but the browser stays open and no run starts.
- **Sessions and challenges:** a job keeps its browser session (cookies, local storage;
  encrypted in Nextcloud, like its account) and replays the sign-in only when the listing shows
  a login form. When a site asks for a person instead (reCAPTCHA, hCaptcha, Cloudflare, "verify
  you are human") the run stops, the job shows **Needs you** and its user gets a notification;
  scheduled runs wait. **Continue in the browser** opens the recorder's browser with the job's
  session at that page: the person answers it, **Done** keeps the session and runs the job.
  Harvester never answers a challenge itself. When a step fails or a page asks for a person,
  the job keeps what its browser showed (a JPEG of the window, at most 400 KB, with the page's
  address and title; in the app's database, the newest per job, deleted with the job): its
  line in the progress log gets **Show what the browser saw** (`GET /api/jobs/<id>/shot`, the
  job's owner only), and from there **Open in the browser** opens the job's browser with its
  session at that page. A failed step's line names its element when it has no text
  (`step 2 (click a.overlay-b:nth-of-type(6)) failed`). A new value for any of the job's account
  forgets its kept session (the cookies, a refresh token the provider replaced): those are the old
  account's, and would be used instead.
- **A person's browser:** sites' bot protection (Cloudflare, ...) refuses a browser that says it is
  automated, or cuts its connection (`ERR_HTTP2_PROTOCOL_ERROR`). Chromium runs as the full browser
  in its new headless mode (not the headless shell), without the automation flag, with Chrome's
  user agent and one language and time zone for recordings and runs (`HARVESTER_LOCALE`,
  `HARVESTER_TIMEZONE`, default `en-US`, `Europe/Berlin`). A website job's **HTTP/1.1 only**
  serves the sites that cut HTTP/2 connections, in the recorder too.
- **Downloads:** a pause between files; waits when the site asks (429 or 503 with
  `Retry-After`, or a wait pattern on the URL it redirects to, like heise's `wait_sec=`) and
  three retries of a broken transfer; never a web page instead of a file; expected file types
  (`pdf`, `zip`, `image`, ..., several separated by commas), a smallest and a largest size. A
  megabyte is 1000 × 1000 bytes everywhere (the largest file's MB, the form, the progress log),
  so 4096 MB is the 4.1 GB the form shows. A file of another type or over the largest size is
  the same next run: it is skipped with a line in the log (*skipped: not of the job's file types,
  or too large*) and not tried again, until the job's types or sizes change. A network failure,
  an error page, a too small answer or a 401 or 403 for one file fail that file, which is tried
  again next run; only when every file of a run (two at least) is refused is it the account's
  trouble (*Sign-in refused*).
  Files go to Nextcloud in chunks of nc_py_api's first kind: its second numbers the first chunk 0,
  which Nextcloud refuses where the storage takes multipart uploads (S3), so every file over 5 MB
  failed with 400.
- **Changed tags:** a job remembers each file it saved, with its ID and the tags it gave it.
  When the job's tags change, a pass right away (no run, the schedule stays) brings them to
  those files: the missing ones added, those the job gave and has no more taken off; tags their
  people added stay, a file deleted since is left. Files saved before 0.8.1, which it did not
  remember so, are found by listing the site once without downloads and looking for each in the
  folder. Its progress log shows the pass. A pass is kept in the database until it goes
  through: one stopped, failed or cut short by a restart of the app comes again (queued when
  the app starts, else at the job's next turn, before its run). A **Run now** asked for before
  the tags changed still runs, after the pass.
- **Repeat runs:** a job remembers what it downloaded; a file already in the folder is tagged and
  not downloaded again. At most `max_items` files per run (`-1`: no limit), the rest comes next
  time; a run ends after two hours in any case. One file that fails does not stop the others (it is
  tried again next run), five failures without a success do; a file whose name cannot be made
  (`{year}` of a release without a date) fails alone, naming the missing value. Two files of one
  name (two invoices of one day, links all called "Download") get two names: a name taken by a file
  the job saved for another item gets "(2)", "(3)", ... after a space before the extension
  (`Rechnung (2).pdf`); a file the job has no record of (put there by hand) is adopted. Once a run
  went through the whole site, a listing of known files ends the next runs (*Stop at known files*):
  of a level's links to follow, the first known one ends it when the level's last is known too; when
  the last has new files the level lists its newest last, and is followed from the end back to a
  known one. A listing over several pages is taken to start with its newest page: for one whose
  first page is its oldest, turn the option off. Only a change of what is listed (the source, a
  recipe, an address, a folder, patterns, the pages or entries read) starts over with a complete
  pass; files per run, file names, tags or the schedule do not.
- **Pause and stop:** while a job runs, **Run now** becomes **Pause** (then **Resume**) and
  **Stop**. Both act before the next file: a paused run waits (its time does not count against
  the two hours; after six hours unresumed it stops), a stopped one ends and keeps what it saved,
  and its next run goes on from there; a stop also ends a wait (a retry, the site asking to wait)
  and a download in progress at once (its chunks, yt-dlp's, gallery-dl's and rclone's: a helper
  program is killed), and the file it was at comes next run.
- **Runners:** `HARVESTER_RUNNERS` jobs run at once (2 by default), each in a thread of its own;
  a job never runs twice at once, and jobs of the same site (the host of the job's address) wait
  for each other, so no site gets more than one job's requests. The schedule and **Run now** only
  queue a job; a queued job asked for again runs once. The page shows it **Queued** until a
  runner takes it, and checks every two seconds while a job of yours waits or runs (ten
  otherwise). Everyone's load is shown, nobody's jobs: above the list a line such as *Runners 2/2
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
- **Admin settings:** *Administration settings > Additional settings > Harvester* sets the
  numbers of jobs and browsers at once (1 to 8 each) and the proxy; the environment's values
  (`HARVESTER_RUNNERS`, `HARVESTER_BROWSERS`, `HARVESTER_PROXY`, `HARVESTER_PROXY_NAME`) are the
  defaults. The app reads them each minute, so a change needs no restart: a lowered number lets
  the running jobs and open browsers finish first.
- **Proxy:** with a proxy a job may go **Through the proxy**: its downloads, its browser, yt-dlp
  and gallery-dl use it, and sites that refuse servers' addresses see the proxy's. HTTP(S), with
  credentials in the address if it needs them (`http://user:password@host:3128`), or SOCKS5
  without a password (`socks5://host:1080`; `socks5h://` has the proxy look up the names), e.g.
  `ssh -D` or Tor. A SOCKS5 proxy with a password, or another kind, is refused (Chromium cannot
  send a SOCKS password): the app's log says why, once, and no proxy is used. The proxy's name
  changes what the form says, e.g. *Through the home connection* for a proxy at home. Without
  one the option is not offered, and a job that chose it goes directly. The recorder follows the
  form's choice.
- **Which address sites see:** the recorder shows it under its address bar ("Sites see
  203.0.113.10 (DE), through the home connection", or "directly, from the server"), and a run
  through the proxy starts its log with it. Both ask Cloudflare's `cdn-cgi/trace` through the
  same connection the pages and downloads use, so it is the proxy's address only when the proxy
  is really used.
- **Notifications:** a job that needs a person, or fails, notifies its user; the same failure
  again on the next runs does not. A one-time job tells what it did as it is deleted.
- **Templates** (`ex_app/lib/harvester/presets/*.json`), jobs with a few parameters, one card
  each on the *New job* page. A parameter may suggest values (`suggestions`: values, or
  `{value, label}` shown by the label, each passing its `check`), offered as buttons on the card:
  every template suggests schedules, the media ones their quality (yt-dlp formats from 480p to
  the best, joined video and audio, or audio alone), others folders, names, magazines, years or
  file patterns:
  - `heise`: heise's magazines (iX, c't, ...) as PDF for a subscription;
  - `pdfs-on-a-page`: the PDFs linked from a page;
  - `mail-invoices`: invoices (PDF attachments) from a sender;
  - `podcast`: a podcast's episodes;
  - `video-channel` and `channel-audio`: a channel's newest videos, or their audio;
  - `video-on-demand`: an on-demand job for the videos you send it, into one folder;
  - `gallery`: a gallery or album's images;
  - `nextcloud-share`: the folder of another Nextcloud's public link;
  - `app-releases`: an application's release downloads, of its newest release.
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
- **Safety:** the browser's pages and the downloads reach only http(s) addresses outside local
  and private networks, checked on the addresses a name resolves to and on every request and
  redirect (a `file://` page would show the app's secret): a page's WebSockets too (one refused
  is never connected), its service workers are off, and a file a recorded site links to is
  downloaded by the app's guarded HTTP client with the browser's cookies, not by the browser.
  Behind those checks, every connection of a browser goes through its own guard, a proxy in the
  app on 127.0.0.1 (`browser/guard.py`; for loopback too), which sees what Playwright's routes
  do not, such as a web worker's WebSockets: it looks the name up once, refuses it unless every
  address is public, and connects to the address it checked, so a name that changes meanwhile
  (DNS rebinding) gains nothing; the home connection is asked for that address, not the name.
  WebRTC sends only through the proxy (no UDP of its own), and WebTransport does not connect
  through one. rclone's sign-in browser alone may open rclone's listener (`127.0.0.1:53682`),
  while rclone runs. yt-dlp, gallery-dl, rclone and mail servers connect by themselves (their
  addresses checked the same way before, not their redirects), as do the guarded HTTP client's
  downloads after their check: there the External Apps' network rules stay the guard. Pages
  and feeds are read up to 16 MB, a file in chunks up to the job's largest size. Chromium, its
  Playwright driver, rclone, gallery-dl and ffmpeg run without the app's secrets in their
  environment (a proxy's password goes to gallery-dl in a 0600 file), and the app's process is
  undumpable, so its `/proc` files (its environment) are root's. The app runs as the user
  `harvester`, frpc as `harvester-frp`, whose configuration (HaRP's key) `harvester` may not
  read, both without a way back to root (`setpriv --no-new-privs`, `start.sh`). A job's id is
  never used again. A person's pattern runs with a time limit, in the recorder (2 s for a page)
  and in the runs (1 s a text: feeds' links, mail's senders, subjects and file names, release
  files, the wait pattern), letting the other threads go on meanwhile; one that takes longer
  fails its job with a message, not the app. The recorder's WebSocket takes only Nextcloud's
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
