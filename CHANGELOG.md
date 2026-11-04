# Changelog

## Unreleased

### Fixed

- The Library's viewer and the page's questions open in the middle of the window, at any width
  (a phone's too), not at its top left; the viewer is as wide as the window allows.
- A move's states from Files are readable in light and dark: tinted pills with the theme's
  success and error text colours, as the jobs' states, not light text on a light green.
- The progress log names where a file went: *Uploaded to Immich: a.jpg (2.1 MB), album “Trip”*,
  *Already in Immich*, *Tagged in Immich*, *Saved in your library*, *Saved in the vault …*,
  rather than *Saved /folder/a.jpg* as if it were in Files. The size is the file's.
- A file name from a value ending in a dot before the extension's (a title "We tried." in
  `{title}.{ext}`, yt-dlp's `{filename}`) has one dot, not `We tried..mp3`.
- A vault's inbox follows the job's *Folder in the vault*: a change after the job's first file
  reaches the inbox's marker with the next file, rather than the vault app moving the files on
  into the folder the marker named first (the vault's top). A marker made in the vault app keeps
  its folder while the job's is `/`.
- *Move to the private library* in Files opens the Library with the move it proposed although
  AppAPI's redirect comes without `?fileIds=`, and so does a reload in the next 15 minutes, until
  the move starts.

## 0.14.0 - 2026-11-04

### Added

- **Tags from lists:** a job's tag that is a list value alone (`{tags}`) gives a tag per
  entry (cleaned, without repeats, 20 at most per file; the log says how many more were left
  out); elsewhere a list's entries are joined with a comma. gallery-dl's lists of texts are kept
  (`{tags}`) and its dicts of them become one each (`{tags_general}`, `{tags_artist}`), yt-dlp's
  `{tags}` and `{categories}` too; the form offers each source's under Tags.
- **Values of the pages a website job opens:** `{page_title}`, `{site_name}`, `{page_date}`
  (with `{page_year}`, `{page_month}`, `{page_day}`) and `{page_lang}`, from `<meta>`, JSON-LD
  and `<time>`, read once per page (one script in the browser, the parsed page over HTTP) and
  passed down to the next levels and the file links. A level's `page_values` read more on the
  page its links open, and a value with `"many": true` is a list (tag chips, cleaned of `#` and
  counts).
- **Recorder:** **Pick tags** beside a level's links (a chip in a row or card), **Pick a value on
  its pages** and **Pick tags on its pages** on a page a level's links open (the chip and those
  like it outlined); each level lists the pages it opened with their values, and *Test the
  recipe* shows each file link's values.
- **Save to** in a job's form: the person's files (as before) or **Harvester's private
  library**, in the app's own storage per person, which Nextcloud's Files, Photos, search,
  WebDAV and sync clients never see, with the admin's limits per person (10 GB by default) and
  in all. A file deleted in the library is fetched again as one deleted in Files (*Download
  deleted files again*).
- **The private library is encrypted**, a hidden space for any file: an X25519 key pair per
  person, its private key wrapped with the library password (scrypt) and a recovery key shown
  once at setup (typed back to confirm); each file's content in 64 KiB chunks of AES-256-GCM
  (ranges decrypt their chunks alone; a chunk changed, swapped or cut off is found), its key
  sealed to the public key, its name, folder, size, type, labels and thumbnail encrypted too.
  Jobs, uploads and moves write while it is locked; reading needs it unlocked, the private key
  in the server's memory only, dropped at lock, after the person's minutes and at a restart.
  **Library** on the page: setup, unlock, recovery, folders, thumbnails (Pillow), a viewer,
  search by name and label, uploads in pieces (drag and drop), Rename, Move, Delete, and a
  cookie (HttpOnly, SameSite=Strict) or per-file tickets instead of a token in addresses.
- **Move to the private library** in Files' actions menu, for files and folders: each streamed
  into the library, checked, the original deleted (its versions with it) and purged from the
  trash bin; a file that fails stays, versions and all, and the page says why. Nothing shared
  with the person and nothing in a team folder is moved. `occ harvester:library-reset <user>
  --confirm`: the admin deletes a person's library.
- **Save to Immich**: a job's photos and videos uploaded into the person's Immich account
  (Immich's API of version 3), streamed, after Immich was asked for the content's SHA-1 (one
  there already is adopted, and still filed and tagged); an optional **Album** by a template
  (`Magazines {year}`), made once and reused; **Immich's tags** as *Tag as* (`a/b` nested); a
  photo deleted in Immich uploaded again; other files skipped for good with why; an unpacked
  ZIP's photos and videos alone. **Your Immich account** in the form: the server and the
  person's API key (a sensitive preference, tested as it is saved, never sent back;
  `GET`/`PUT`/`DELETE /api/immich`, `POST /api/immich/test`). The admin's **Immich server**
  (`HARVESTER_IMMICH_URL`) is preset for everyone and reached directly; a server a person types,
  through the guard's check; `https://` unless **Immich over plain http**
  (`HARVESTER_IMMICH_HTTP`).
- **Save to** a Cryptomator vault: **its inbox** (the folder gets the marker of the app *Vaults
  for Cryptomator*, which moves the files into the vault when the person unlocks it; files moved
  out are never fetched again) or **the vault directly** (each file encrypted on the server and
  written over WebDAV, `harvester/vault.py`; the password typed once, only the vault's keys kept,
  as a sensitive preference; a prominent warning that the server then holds the vault's keys).
  `PUT /api/jobs/<id>/vault` (the password), `GET` (whether the keys are kept), `DELETE`.
- **Tag as** in a job's form: Nextcloud's tags (as before), **Harvester's labels** (in the app's
  database, shown and filtered in the job's **Files** and the library, never a Nextcloud tag)
  or none. `GET /api/jobs/<id>/files`: the files a job saved, with their tags or labels.
- **Browse** beside the address of a gallery, video, feed or release job: the recorder's browser
  alone (nothing recorded), at the field's address, and **Use this address** takes the page it
  shows into the field.
- **Try the address** in the form of every job but a website's (*Try the mailbox*, *Try the
  remote*): the listing once as the form holds it, before it is saved, nothing downloaded
  (`POST /api/try`): what a run would fetch (Files per run), what the job's history has, what
  its file types, sizes and patterns leave out, the first 20 files with the names and tags they
  would get, a mailbox's messages with what each gives, or why it failed with a hint. In a
  runner's place, through the guard, the proxy and the DNS servers, 60 seconds at most, one try
  of a person's at a time.
- DNS servers of the admin's choice: **DNS servers** (a form of its own in Harvester's admin
  settings, or `HARVESTER_DNS`) sends every name the container looks up (the app and its guard,
  the browsers, the downloads, yt-dlp, gallery-dl, rclone, the mail servers) to plain addresses,
  DNS over TLS (`tls://host[:port]`) or DNS over HTTPS (`https://host[:port]/path`), a ClientID in
  the name or path, e.g. a filtering AdGuard Home. AdGuard's `dnsproxy` (in the image, pinned
  and checked) runs on `127.0.0.1:53` and `/etc/resolv.conf` names it; Nextcloud's, HaRP's and
  the proxy's names stay Docker's. Servers that do not answer fail the lookups, unless **Docker's
  DNS when the servers fail** (`HARVESTER_DNS_FALLBACK`, off by default) is on. The form's
  description is the status: whether the proxy runs and a test lookup works.
- A name the DNS servers block (they answer `0.0.0.0` or `::`) is told as such in the job's log,
  by the browser's guard and the downloads' checks (*blocked by the DNS server*), and a name they
  do not know as possibly blocked.
- **Fetch** first in a mail job's form: *Attachments*, *Linked files* or *Both*. The form shows
  only the chosen part's options (an option's `shown_when`; a group whose options are all hidden
  goes with them) and keeps the others' values; a *Linked files* job asks for a link address or
  link text and nothing of attachment names. A job saved before keeps what it fetched (its
  attachments, and its linked files when *Also the files linked in the message* was on), read so
  (`Source.upgraded`, also for `GET /api/jobs/<id>` and a recipe file of before), not
  rewritten. A choice's words in the form (an option's `labels`).
- A mail job's folder the mailbox does not have: the run's error names the folders it has, the
  closest first, ten of them (*The mailbox has no folder “Rechnungen”. It has:
  INBOX/Rechnungen, Archive, INBOX*); **Try the mailbox** checks the folder first and offers the
  mailbox's folders to take into the field (`folders` in `POST /api/try`'s answer).
- A mail job's run whose new messages are none of the sender and subject says so as a warning
  naming the patterns (*Folder INBOX: none of the 12 new messages is from “…” with a subject
  like “…”*).
- Under a mail job's **Pages to the on-demand job**, while you have no on-demand website job:
  how to make one (record one for the portal, *Website*, *On demand*).

### Changed

- The recorder's patterns in plain words: **Address must match**, **Text must match** (a pick
  by its text) and **File's address must match (optional)** (a JSON listing), each with a line
  saying that a part written `(?P<name>…)` becomes `{name}` for file names and tags, with an
  example.
- A mail job's **Pages to the on-demand job** says what it is for in one sentence.
- The progress log names the address a run looks at (*Looking for new files at https://…*) and
  says how many of the listed files the job has already (*37 listed files are known already: not
  downloaded again*), and the run's outcome counts them (*0 downloaded, 37 known*).
- A website job's progress log names each page of file links with what it matched (*page 2
  (https://…): 12 of 40 links match, 3 new*, 20 pages a run) and says why a listing ended: a page
  of known files only (*Stop at known files*), or the *Pages* limit with a next page still there
  (*Read 10 pages (Pages): older pages are not read this run*). A level's *waiting for … to show*
  is said once a run, not for each of its listings.
- Feed, video, file store and release jobs say in the log what their listing found: *The feed
  “Reports”: 20 entries, 18 file links*, *30 entries listed (Entries per run: 30)* and the live
  or upcoming ones skipped, *rclone listed 12 files in share:Reports* (or that *Files* took
  none), *5 releases looked at (2 drafts or prereleases left out), 7 files match*.
- A download's waits and retries are lines in the log: *… asks to wait 60 s* (429, 503, a
  wait pattern) and *…: the connection failed, trying again in 10 s*, not a silent pause.
- A video job's log has yt-dlp's warnings (ten a run, each once), and yt-dlp's advice for its
  command line is said as the job's options: *Use --cookies-from-browser or --cookies* reads
  *Set the job's account: Cookies*, likewise its user name and password, its formats and
  impersonation.
- A watched page's run outcome says what changed: *The page changed: -Tea: 2 EUR +Tea: 2.50 EUR*
  (one line, 100 characters at most; *The page changed* alone before).
- A mail job's progress log says what the mailbox search found (*Folder INBOX: 12 new messages,
  3 from the sender and with the subject asked for*, or no new messages and how many were done
  with before) and names each message of the sender and subject, with what the job takes from it
  (*1 attachment, 2 links*) or why nothing (*nothing to fetch: no attachment of the names, types
  and sizes asked for, no link the patterns take*): up to 50 a run, the rest counted.

- The app's routes in groups: Try the address, recipe files and templates and the rclone wizard
  in `harvester/routes/` (FastAPI's `APIRouter`), the jobs and the recorder in `main.py`; the API
  is the same.

### Fixed

- A long run (a first one of hundreds of files) no longer pushes its own start out of the
  progress log: its first 30 lines stay beside the job's last 500.
- A video larger than the job's largest file that yt-dlp finds so only as it downloads is skipped
  for good as one known to be too large (*yt-dlp saved no file for …: larger than the job's
  largest file*), not failed as *yt-dlp left 0 files* in every run.
- A release job whose forge answers with an error or cannot be reached fails with its host,
  answer and what was asked (*github.com answered 502 for the releases of o/r*), or the address
  it could not reach, not with httpx's text; the log says how many releases were looked at and
  how many files match.
- A gallery job whose gallery-dl lists part of the gallery and then fails (a page of many) says
  so in a warning with gallery-dl's error, and the run no longer counts as a complete pass; the
  log says how many files gallery-dl listed.
- A run whose listing breaks after files came (a next page that fails, a feed cut short) still
  fails, but its reason keeps what it did: *3 downloaded; then the listing failed: …* (only the
  listing's error before).
- A linked file whose site cannot be reached (a name that does not resolve, a failed connection,
  an error 5xx, a redirect to a private address) no longer fails a mail job's run when nothing
  else came: it is skipped this time with a line in the log (*skipped this time* in the run's
  outcome), tried again in the next runs and given up after the third.
- A file's failure in the progress log (*file: why*) and a selector's repair (*level 1: selector
  repaired (… → …)*) are translated (German, Polish), as their neighbours were.
- The usual reasons a file fails or is skipped (*… answered 404*, *… is not of the job's file
  types*, *… is larger than the job's largest file*, *… gave only 12 bytes*), a run's *(and 3
  more)* and a file store's configuration errors are translated (German, Polish).
- The run's reasons *the start page has none of the recipe's links* and *… asks for a person*,
  and the log's line of a page that loads none of the types to capture, are translated (German,
  Polish), as their neighbours were.
- A recorded click that closes its own window (a sign-in popup's **Allow**) no longer fails its
  step now and then: the window may close before the browser reports the click as done.

## 0.13.2 - 2026-11-04

### Added

- **Light browser** (website jobs; on for new jobs, off for those saved before): the job's
  browser skips images, fonts and videos while it walks the site, each of which came through the
  browser's checks uncached; pictures' links are still found by their addresses. Ignored for a
  disguised browser and for jobs that save pages or capture the files they load. A picture of a
  failed step shows the pages without their images then.
- Files linked in an e-mail: a mail job's **Also the files linked in the message** (off by
  default) downloads the files its messages link to ("Download your invoice"), from the HTML
  part's links and the plain text's addresses, of the **Link address** or **Link text** pattern
  (one at least), at most 10 a message; never `mailto:`, `tel:`, an unsubscribe link or a private
  address. Through the checked download (public addresses only, at most 10 redirects, the job's
  types and largest file), named by `Content-Disposition` or the final address, known by the
  message's ID and the link's address. A link that gives a web page is sent to the mail job's
  **Pages to the on-demand job** (an on-demand website job of the person, whose recipe signs in),
  else logged as *a page, not a file*; such a page and an expired link (404, 410, 401, 403) are
  logged once and skipped for good.
- Deleted files downloaded again: **Download deleted files again** (every job; on, also for the
  jobs saved before) has a run ask Nextcloud, before it skips a file the job saved already,
  whether the file is still there, by its ID: one deleted or in the trash is downloaded again
  (*Deleted in Nextcloud, downloaded again: …*), one moved or renamed counts as there. Only the
  files the listing finds again are asked for, a listing page's at once (one WebDAV `SEARCH` per
  100 files at most, each once a run); a file without an ID by the path it was saved at. When
  Nextcloud cannot be asked, nothing is downloaded and the log says so. An unpacked ZIP counts as
  there while its archive or its folder is.

- More consent managers whose cookie banners **Dismiss cookie banners** answers by their own
  reject buttons: Sourcepoint (its message's frame, never the page itself), CookieYes and its
  older CookieLawInfo, Complianz, Osano, Iubenda, consentmanager.net, Cookie Script, Termly,
  Axeptio, Cookie Notice and Google's Funding Choices (*Do not consent*); a recorded click on
  one of their banners is optional then.
- The recorder adds approvals and handovers: **Add step: wait for your approval** (once the
  approval went through, until the page's address then) and **Add step: hand over to you**
  where the recording is; the Steps list changes their label, an approval's wait and their
  `until` (an address's pattern, **this page’s address**, an element typed or picked with **Pick
  an element**).
- Cookie banners dismissed in the progress log: an info line on the page where it happened,
  naming the consent manager (or *a cookie banner*) and the button clicked, once a manager in a
  run.

### Changed

- The app asks Nextcloud and its database less: the admin's settings are read each minute with
  one client (a new one each minute before, never closed), a job's account in one request (one
  per field before, twice a run), the weekly tests' times in one query a minute (one per job),
  and the progress log's poll and the jobs' list read the waiting addresses once (twice).
- The jobs page asks for less: the list comes without the jobs' options (`GET /api/jobs?brief=1`
  gives each job's own address instead; a recipe may be 100 KB), a job's form asks for its own
  (the new `GET /api/jobs/<id>`), and the progress log keeps its lines when the list is drawn
  again (a run that starts or ends), asking only for newer ones, and the queue's status from its
  poll. `GET /api/jobs` without `brief` still gives every job's options (Talk AI reads them).
- The recorder's **Network** panel keeps at most 8 MB of its answers' bodies together (60 MB a
  recording before: 30 answers of up to 2 MB each), dropping the oldest first; their entries
  stay, and the Preview of one dropped says it is no longer kept.
- A website job with the browser downloads its files through one checked HTTP client for the
  run (its connections kept), instead of a new one for each file (some 30 ms, a new TLS
  handshake, and a copy of the browser's whole session each): each request still gets the
  browser's cookies for its address as they are when it is sent, and every address and redirect
  is checked as before.
- Listing pages in the browser are read once their level's links are there (a next page once
  they changed), then after two seconds at most for what a script still loads, instead of half a
  second and up to 15 seconds more for the network to go quiet, which a page with analytics or
  polling never does: three such pages took some 48 seconds, now some 7. On such a page a scroll
  or a **Load more** round waits up to three seconds for the page to grow instead (a feed of
  three rounds: 79 seconds, now 10). A page whose links do not show within five seconds still
  gets the network's whole wait.
- The browser's screen comes as binary WebSocket messages, each frame the JPEG itself, instead
  of base64 in JSON (a third larger, and parsed): the page shows each by an object URL and lets
  the one before go.
- The browser's screen in the recorder (and in *Continue in the browser* and rclone's sign-in)
  takes less of the server's CPU: every second frame of Chromium's (some 30 frames a second)
  instead of every frame, and none while the jobs page is hidden (another tab, a minimized
  window), again at once when it shows. A view left for another one of the app closes its
  browser at once, instead of after 15 minutes without input.
- An address sent to an on-demand job again brings back its files deleted in Nextcloud since,
  whatever the option, and the log says where each file fetched before is now (*Fetched before,
  in your files at …*).
- A deleted file's record of its content (and the duplicates' that named it) goes when the file
  is found gone, so that its download anew is no duplicate of itself; a known file due for its
  check of changes that is gone is downloaded anew instead of staying away.
- Saving a file asks Nextcloud less: a file under 5 MB is one `PUT` (larger ones still go in
  chunks), its ID comes from the answer instead of a lookup, and a job's folders are made once a
  run, not for each file (one made again if it is deleted meanwhile): some 2 requests a file
  instead of about 9.
- The pause between two downloads counts from the end of the site's last answer (a file, an
  error, a check for changes, a sign-in anew), so that saving and tagging the file are part of
  it: the site still sees the whole pause between two downloads, and a run waits about a second
  less a file.
- The jobs page asks for the progress log every 30 seconds while none of your jobs runs or
  waits (10 before), and not at all while the page is hidden (another tab, a minimized window):
  at once when it shows again.
- A page's JavaScript in the job's browser and the recorder's has at most 1 GB of memory (some
  3.7 GB before), so that a page that runs away fails by itself rather than taking the memory of
  the other browsers and the app.
- The image is some 270 MB smaller: without Playwright's headless shell, which nothing starts
  (the browsers are the full Chromium); an empty `HARVESTER_BROWSER_CHANNEL` means it too.
- The app's modules are compiled in the image, not again at each start (some 0.25 s).
- The container's health check runs every 15 seconds instead of every 2 (a shell and `pgrep`
  each time, for the container's whole life): a deploy waits some 15 seconds for *healthy*
  (HaRP waits up to 90); failures in the first five minutes do not count, then four in a row
  make the container *unhealthy*.
- The jobs' page on a phone (narrower than 700 pixels): each job a card, its columns one below
  the other with their names and its buttons on a line of their own, instead of a table wider
  than the screen; the header's parts one below the other, the progress log's text below its
  time, and a line of an earlier day with a short date and no seconds. The table's last column
  is named *Actions* for screen readers.
- The recorder: **Test the recipe** and **Test as the job runs** in its toolbar beside Done
  (below the steps and picks before); a test's result starts with its outcome in one line,
  coloured by it, and is scrolled into view. On a narrow window the screen is above the steps at
  the full width (a sliver beside them before), and the toolbar wraps, Done and Cancel together.
- The job's form shows a source's essentials (a website's recipe, file name, file types and
  files per run; another source's address, file names and what it takes) and folds the other
  options into **Browser and connection**, **Files and sizes**, **Repeated runs** and a mail
  job's **Linked files**, each opened by itself when one of its options differs from its
  default, saying how many do (*(2 changed)*); a website job's form was 21 options in a row.
  `GET /api/sources` gives each option its `group` (empty: an essential). Save and the form's
  other buttons stay at the window's foot while the form scrolls.
- The recorder's step buttons are **Add step: scroll to load more**, **Add step: wait for your
  approval** and **Add step: hand over to you**, on a line of their own below the modes; outside
  Record steps they are off and say *Choose Record steps to add steps* (only a tooltip before).
  The modes are a group named *Mode* for screen readers, and the Steps list's **↑**, **↓** and
  **✕** are buttons of 34 pixels (a few pixels before), its *optional* kept with its checkbox.
- The signs of the recorder's step marks (▶, ⏸, ↻, ✓, –, ✗, ↷, ■), of a pattern's or an
  address's check (✓, ✗) and of *✔ will be used* are no part of their translated texts: shown
  beside them, not read out by screen readers.
- Delete, a template's Remove, Back to the jobs with an account typed and the job's browser's
  *Into the account* ask in the page, in a dialog of Nextcloud's look (the browser's own
  `confirm()` before); Escape is No, and a deletion's dialog has the focus on Cancel. The
  recorder's **Clear picks** offers **Undo** for 15 seconds, and its **Cancel** asks before it
  drops changes to the recipe.
- Buttons alike: the actions in a row or a line of text (a waiting address's *Cancel*, *Show
  what the browser saw*, a cookie's *Show*, the Steps list's *this page’s address*, *Pick an
  element*, *make optional?*) are borderless buttons as Nextcloud's tertiary ones (underlined
  links before), and red is kept for what cannot be undone: a pick's, a level's and a value's
  *Remove* in the recorder are plain buttons (*Undo pick* takes them back).

### Fixed

- Gallery jobs (again): urllib3-future, which nc_py_api's niquests brings, is built without
  installing itself as `urllib3` (`URLLIB3_NO_OVERRIDE`), so gallery-dl gets upstream urllib3 again.
  0.13.1's urllib3 2.7.0 did not help: the fork stood in its place, and lacks
  `urllib3.connection.create_urllib3_context`.
- *Test the recipe* in the recorder failed at a recorded click on a cookie banner that **Dismiss
  cookie banners** had answered already; it skips the click as optional, as a run does.
- The jobs' states *OK*, *Failed*, *Sign-in refused*, *Changed*, *Needs you* and *Paused* were
  their text colour on their full colour, hard to read in both themes: a light tint of the colour
  behind the text now.
- The browser views' screen kept the keyboard: Tab, Shift+Tab and Escape went to the site, with
  no way out but the mouse. Escape twice within a second leaves it for the page's next control,
  which a hint says while the screen has the keyboard; the screen's picture is no longer a stop
  of its own for Tab (its hidden keyboard field is).
- Screen readers: the progress log was a live region read out at every poll, and the queue's
  load line a status; the log's errors and outcomes are announced through one region now, as is
  the recorder's test (its start and outcome), whose result was a new live region each time.
  Errors are alerts, and a refused Save marks the field it names invalid, described by the
  error, its group opened and the keyboard in it.
- The page was blank until the app gave its sources and templates: it says *Loading…*. Save says
  *Saving…* while the app answers, and a mail job's *Pages to the on-demand job* says when your
  jobs could not be read (an empty list before). A required field's name (a job's Name and
  Folder) is marked, and a field's help is no longer part of its label but its description.

## 0.13.1 - 2026-11-03

### Added

- Watching a page: a website recipe's `"watch": {}` (or `{"selector": "…"}`) saves no file but
  compares the start page's text (or the selector's) with the last run's, whitespace aside; a
  change is a line in the log and a notification with an excerpt of the lines gone and come (at
  most 300 characters), and the run's outcome says whether the page changed.
- ZIP files unpacked: **Unpack ZIP files** (every job, off by default) puts a downloaded `.zip`
  file's files into a folder of its name beside it, tagged; **Keep the ZIP files** (on by default)
  keeps the archive too. A suspect archive is saved as it is, the log saying why: a path out of
  its folder or an absolute one, a symbolic link, an encrypted file, two files of one name, more
  than 1000 files, more than the job's largest file in all, more than 100 times its packed size
  (counted as it unpacks). No nested unpacking; `.epub`, `.docx` and other ZIPs of another name
  stay files.
- Duplicates by content: a job keeps the SHA-256 of each file it saves; a file whose content one
  of its files from another address has is not saved again, and the log and the run's outcome
  say so (*A duplicate of …*, *1 duplicate (not saved again)*).
- Versions: **Check known files for changes** (website and feed jobs; on for new jobs, off for
  those saved before) asks the addresses of the files a job saved again, each at most once in 20
  hours: over HTTP with `If-None-Match` and `If-Modified-Since` (304: no download), else by the
  downloaded file's hash. A changed file is saved over its own, so that Nextcloud keeps the one
  before among its versions; a deleted file is not brought back, a failed check fails nothing.

### Fixed

- Gallery jobs: urllib3 is held at 2.7.0, since 2.8.0 no longer has the function gallery-dl
  calls (every gallery job failed with `AttributeError: module 'urllib3.connection' has no
  attribute 'create_urllib3_context'`).

## 0.13.0 - 2026-11-03

### Added

- Cookie banners dismissed on their own: a website job's **Dismiss cookie banners** (on for new
  jobs, off for those saved before) has its browser answer OneTrust, Cookiebot, Usercentrics,
  Didomi, Quantcast (TCF), TrustArc, Borlabs and Klaro with "Reject all", else "Only necessary",
  and a button of exactly such a text (German, English, Polish) in a dialog or a floating banner
  that speaks of cookies; in the steps and the walk, in frames and shadow roots, and in the
  recorder's browser, which records no step of it. Nothing else is clicked; a recorded click on a
  banner is optional then.
- Approvals in an app: the step `{"action": "approve", "until": {"url", "selector"}, "label",
  "wait"}` shows **Needs you** with its label and notifies the job's person, then waits until the
  page shows `until` (an address's pattern, an element, or both) that the approval went through,
  or fails after its wait; Stop ends it, and a page that shows `until` already asks nothing.
- Handing over to the person: the step `{"action": "handover", "label"}` stops the run as a page
  asking for a person does, keeping the browser's session as it was there: **Continue in the
  browser** opens the page, the person does what only they can (a passkey), and **Done** runs the
  job, whose kept session skips the sign-in. Its optional `until` skips it when the page shows
  it already; a recipe with a handover needs its `start`. Approvals and handovers end the sign-in
  a kept session skips.
- Codes from the phone: the step `{"action": "ask-code", "selector", "label", "wait"}` pauses
  the run at a code Harvester cannot read itself (an SMS, a call, an authenticator it has no
  secret of): the job shows **Needs you** with a field **Enter the code from …** on the jobs page
  and notifies its person, types the code sent from there into the field and goes on; after the
  wait (10 minutes by default, 30 s to 30 minutes) the run fails cleanly, and Stop ends the wait.
  The code goes only to the job's own run while it asks (`POST /api/jobs/<id>/code`) and is never
  logged or kept. The recorder's Steps list offers **code from the phone?** on a typed code, and
  *Test the recipe* asks for the code below the steps.
- Exports a site prepares first: a level's or the file links' `"wait_for": {"selector",
  "timeout", "reload"}` looks for the element of the ready file on each page (reloading it every
  `reload` seconds if asked) for up to 30 minutes, then reads and downloads its links as before,
  clicked ones too.
- Choices: the step `each` goes through every option of a `<select>` or a custom dropdown, or a
  fixed list of values (every account, year, child), doing the steps after it and the walk for
  each, the choice's text `{choice}` in the file names, tags, key and addresses. The recorder's
  Steps list offers **Repeat for each option** on a recorded choice, and *Test the recipe* lists
  the choices it goes through.
- JSON listings asked for with POST: a form's or a JSON body (GraphQL with its variables), whose
  pages are counted in the body or chained by a cursor the answer gives (`endCursor` with
  `hasNextPage`, `nextCursor`, an answer's next page's address); the headers the page sent that
  matter, a token that changes read when the job runs from a cookie, a `<meta>` or the page's
  storage, never written into the recipe. The Network panel lists POST answers too, with a
  GraphQL operation's name, and proposes all of it from one.
- The recorder's Network panel shows an answer's content: **Preview** beside each one, its JSON
  indented (the first 20 000 characters).
- File links that are clicked (`"click": true`) give the file also when the click opens a tab
  or popup that shows it (a PDF in the viewer, a picture) or starts its download, or takes its
  own tab to it; checked as any download, never a web page, and the tab goes back to the listing.
- Levels to follow that are clicked (`"click": true`): rows, cards or buttons without an address
  that open an entry. A run clicks each, reads the next level where it lands (another address, a
  new tab or popup, a single-page app's route, a dialog) and returns to the listing (Back,
  Escape, else the listing opened again with its loading of more); a click that fails is a gap.
- The recorder picks elements without an address (buttons, links to `javascript:`, rows and cards
  a script opens) as file links or links to follow that are clicked: their pattern proposed on
  their texts, the panel saying the job clicks them, **Click in the browser** to switch it; *Test
  the recipe* walks such levels by clicking and lists clicked file links by their text.
- Pages saved as files: the file links' `"save": "pdf"`, `"html"` or `"markdown"` opens each page
  they lead to in the job's browser (also what an element clicked shows), settles and scrolls it,
  and saves it: Chromium's print (A4), one HTML file with its stylesheets, pictures and frames
  inside (Chromium's MHTML snapshot, its resources as `data:` addresses), or its main content as
  Markdown (Readability's way, on BeautifulSoup). Named by `{title}`, with the extension; checked
  as a download (the job's file types must take it, `markdown` a new one). A recipe's own
  `"save"` saves its start page; the template **Save a page** does it for the pages sent to an
  on-demand job (*Fetch a URL…*, *Send to Harvester*). The recorder's file links pick has **Save
  the pages as**, and *Test the recipe* lists the pages it would save.
- Files a page only shows: the file links' `"capture": ["application/pdf", "image/*"]` keeps the
  files of those types each page loads (a viewer's PDF, pictures), embeds (`<embed>`, `<object>`,
  frames: fetched with the browser's cookies) or holds as `blob:` addresses (read in the page); at
  most 20 a page and 64 MB each, checked as downloads; a page whose files were all placed is not
  opened again.
- Pictures picked as file links (`img`, `picture`) are taken at their full size: the largest
  candidate of `srcset` and `<source>`, an attribute naming the full size, the lazy-loading ones
  (`data-src`, `data-original`, …) over a placeholder `src`; in the browser, over HTTP and in the
  recorder.

### Changed

- A level's pick tells a link's address apart from `#` and `javascript:` (no address).
- Releases through the shared action `nextcloud-app-release` of `dawid/ci-actions`: the release's
  text is the version's section of this changelog, and a tag without one fails.

## 0.12.0 - 2026-11-03

### Added

- Screenshots of a demo Nextcloud in the README and in `appinfo/info.xml`.
- `docs/user.md`, `docs/admin.md` and `docs/developer.md`: the README's details moved there,
  with a section on the backup of Harvester's database; `tests/test_readme.py` checks them too.
- `AGENTS.md`, `SECURITY.md`, `.editorconfig`, and SPDX lines in the Python and JavaScript
  sources.
- CI: `appinfo/info.xml` against the app store's schema (without AppAPI's routes), markdownlint,
  and the AI review of a pull request on request.
- Recipes: a step's `"pause"` (seconds before it) and `"retry"` (`{"times", "delay"}`: tried
  again when it fails), both ended at once by a stop and edited in the recorder's Steps list
  (**pause, retry**); a run logs its pauses and tries.
- **Test the recipe** shows its progress on the recorder's Steps: the step playing, its attempt
  or pause, and each step passed, skipped or failed; a click on the failed one opens its pause
  and retry.
- German (`de`, and `de_DE` with *Sie*) and Polish translations in `l10n/`, as the PHP apps
  have them: the page (texts with placeholders and plurals), the sources', options' and
  templates' titles and help, the admin's settings, the API's plain errors, and the
  notifications, which Nextcloud now translates for each person (their values as rich
  parameters). Other languages are English. `scripts/l10n.py` collects the texts and writes the
  `.js` files; `tests/test_l10n.py` checks every text in every language, with its placeholders,
  and the page's tests run in German too.
- The server's texts with values are translated too: the schedule in words, why a job waits,
  the progress log's usual lines, a run's outcome, a changed site's reason and the recorder's
  warnings come as templates (`harvester/l10n.py`: whole sentences, counts with their plural
  forms, lists joined in the person's language) beside their English, which the API, the logs
  and the database keep (`log.message_l10n`, `jobs.last_message_l10n`); lines from before stay
  English. `scripts/l10n.py` finds them (`said()`, `said_n()`).

### Changed

- The page counts in words that fit the number ("1 file link on 1 page"), and the recorder says
  "level 2" instead of "2nd level"; so does the progress log ("1 file this run").
- The schedule in words: "every 15 minutes" without ", every day", "on days 1 and 15 of every
  month"; a combination of fields without words of its own names the fields.
- The help of the admin's setting *The proxy's name* gives an example instead of
  `Through <name>`, whose `<name>` Nextcloud's settings page dropped as a tag.
- The README in the structure the four apps share.
- `appinfo/info.xml`: indented with 4 spaces, in Talk AI's order, with `<website>`; Nextcloud up
  to 36.
- CI runs on Renovate's branches (`renovate/**`) too, so their updates are merged once checked;
  its steps are named as sentences.
- The README's example of the address sites see is a documentation address (203.0.113.10).

## 0.11.0 - 2026-11-03

### Added

- **Disguise the browser** (website jobs, off by default): the job's browser, its *Test the
  recipe* and **Continue in the browser** start with Patchright (Playwright patched against bot
  checks: none of Playwright's globals in the page, its scripts in isolated worlds, no
  automation switches) instead of Playwright, with the same Chromium, guard and options; a new
  recording takes the form's flag. For sites whose bot check refuses the browser otherwise; it
  answers no challenge. A page's WebSockets are checked by the guard alone (Patchright applies
  no WebSocket route), so no disguised browser starts while the guard is off. The image gains
  some 20 MB (Patchright shares Playwright's Chromium and Node.js).
- **Impersonate a browser** (videos, galleries, websites, feeds, releases): yt-dlp's own
  impersonation (`curl_cffi`, now in the image: extractors that need it, Dailymotion's say, use it
  by themselves) for all of a job's requests and the generic extractor (`generic:impersonate`), for
  sites behind Cloudflare's bot check that answer yt-dlp with 403; gallery-dl's browser emulation
  (Chrome's headers and TLS ciphers). A yt-dlp error asking for impersonation names the option.
  Harvester's own requests too, for websites (listings over HTTP and files), feeds, releases and
  gallery images: httpx over `curl_cffi` impersonating Chrome, each redirect still checked, the
  cookies, headers, size limits and proxy the job's.
- **Steps in frames:** the recorder records what is done inside frames, cross-origin ones (a
  consent banner, a sign-in) and frames in frames included; such a step keeps the frame's path
  (`"frame"`: each frame's element, its address without the query, its name), and a replay
  finds the frame by its element, else by its address or name. The recorder shows where one
  acts (**Acting in: the page** or a frame and its level), the Steps list and the log's step
  labels name the frame. Picks inside a frame are refused with a message and the frame's
  address.
- **Optional steps:** a step with `"optional": true` (the switch **optional** in the recorder's
  Steps list, a hint **make optional?** at clicks that look like consent buttons) waits 5 s at
  most and is skipped when it fails: a line in the log, no picture, no failure. *Test the
  recipe* lists the skipped ones apart from its problems. Recipes without either run as before.
- **Cookies panel:** the recorder and *Continue in the browser* show the browser's cookies
  (this site's or all, by domain, values hidden until *Show*), to change, delete (one, or a
  domain's) or add, checked by the app (no control characters, sizes a browser keeps, only the
  domains the browser is at or has cookies of). *Into the account* takes them as a cookies.txt
  into the job's *Cookies* field (saved with the form, or stored after a confirmation in the
  job's browser); *Use for this job* keeps the session for the job without ending the browser.
  A cookies.txt keeps the HttpOnly mark when read and written.
- **Values from the page:** a level of links or the file links may take values from the page
  around each link (its text, an attribute, an element of its row or card), narrowed by a
  pattern, as text, numbers or dates (English, German and Polish forms, with year, month and
  day), for file names and tags. The recorder's **Pick a value** proposes them from a click near a
  link and shows them in the examples.
- **Listings in JSON:** the file links may be the items of a JSON answer (a site's XHR), with the
  fields of each file's address, text and values and a page parameter; read over the guarded
  HTTP client with the job's cookies, with size, depth and item caps. The recorder's **Network**
  panel lists the page's JSON answers and proposes a listing from the one chosen.
- **Codes by e-mail:** a step types a code the site sends by e-mail, from a new message of a
  sender and subject in the account's *Mailbox for codes* (IMAP, read-only; the job's own
  username and password without one of its own), waiting up to 3 minutes; the code is never
  logged.
- **Test as the job runs:** *Test the recipe* of a saved job can run as the job does: a new
  browser context at the recipe's window (recorded with it; jobs use it too), the job's stored
  account resolved by the app, the form's file types and names, and the first 64 KB of the first
  file fetched to tell a file from a sign-in page.
- **The site changed:** a website job's runs keep what each level of the recipe found; a level
  that found links before and finds none, or whose links no longer match its pattern, ends the
  run *Changed* with the level, its selector and a picture of its page, and its person is told
  once. *Test the recipe* from a changed job's row runs the recipe's test as a job's turn
  (nothing downloaded), and the option *Test the recipe weekly* does so on Sundays.
- **Recipes as files:** *Export recipe* in a job's form downloads its recipe and settings
  (without the account, session or folder); *Import a recipe* on the *New job* page checks such a
  file and opens the form filled in; *Save as template* keeps it as a template of one's own.
- **Browser network guard** in the admin's settings (and `HARVESTER_GUARD`): on by default; off,
  the browsers connect by themselves as before the guard (the home connection given to
  Chromium), the routes still checking what Playwright sees, while a web worker's WebSockets and
  DNS rebinding are left to the External Apps' firewall. For the browsers started afterwards;
  switch it off only when that network is already firewalled from private networks and the
  cloud's metadata service.
- **Selectors with alternatives:** the recorder finds each step's element by several selectors
  that matched it alone (a test id, a stable id, its role and name, `aria-label`, `name`,
  stable classes, its text, its place last), keeps the best and the others as
  `"alternatives"`; picks keep alternatives that matched the same links. A replay tries them
  in turn when the selector finds nothing, and the log says `step 3: selector repaired (old →
  new)` (levels, file links, the next page alike; **Test the recipe** lists them). The recipe
  is not changed; recipes without alternatives run as before. The pick panels list a pick's
  alternatives.
- **Tokens in addresses:** the recorder warns of a step's or the listing's address carrying a
  token of the visit (`code`, `ticket`, `sid`, a signature, …), likely spent when the job runs.
- **Kept sessions:** a kept session skips the sign-in only (the steps up to the last credential
  and what sends it); the steps from there to the listing always run. A session that no longer
  reaches the listing is given up for a sign-in anew, in a browser of its own, and forgotten if
  that fails too; a recipe with other steps or another site forgets it when saved. A session
  that ends in the middle of a run signs in anew once, and the file is asked for again; ten
  failures in a row end a run.
- **Recipes:** dates in a fill step's value (`{today-30d:%d.%m.%Y}`, `{year}`, `{month:02d}`, in
  the job's time zone); a key template for the file links (`"key": "{year}-{issue}"`); file
  links that are clicked to download (`"click": true`: scripts, forms, blobs), checked as any
  download; levels that load more of their pages (`"scroll"`, `"load_more"`). A recipe is
  checked when saved: the kinds of its parts, actions, credentials, keys, dates, the next page's
  level and the key's values, with readable messages.
- **Downloads:** `{disposition}`, the name the site gives the file (Content-Disposition, RFC
  6266 and 5987), made safe; the listing as `Referer`; a page standing in for the file (a meta
  refresh, one download link) followed once.
- **Editing the steps:** the recorder's Steps list moves (↑ ↓) and removes (✕) any step, sets
  what a fill fills in (a value of its own, edited there, or the account's user name, password
  or one-time code), edits a list's choice and drops a step's wait. A recipe with steps opens in
  *Browse*; recording on it asks where the new steps go (*Append* after a step, *Replace from
  there*, *Replace all*) instead of replacing the form's steps.
- **More that is recorded:** a list's choice (a step `select`, by value or by its text), the
  keys of a combobox or autocomplete (↑ ↓ Escape Tab Enter), text edited in place
  (contenteditable), a menu that opens under the mouse (a step `hover` before the click),
  elements in open shadow roots, and a sign-in's popup (steps `popup` and `opener`; a job waits
  for the popup and acts in it). One-time codes are recognised also as Microsoft's `otc`,
  Nextcloud's `challenge`, a plain `code` or a field of 6 to 8 digits after a password.
- **Waits:** a click or press that went to another address waits in a job for that address and
  the next step's element, then briefly for the network, instead of up to 15 s for a page that
  never goes quiet. Recipes without it wait as before.

### Changed

- **The browser's guard is fast under load:** it runs in a process of its own (without the
  app's secrets, ending with the app), so it no longer waits for the interpreter while runners
  and recordings keep the app busy; it relays in larger chunks with one idle timer per
  connection, keeps a name's checked addresses for a minute (one lookup for the connections a
  page opens at once; it still connects only to addresses it checked), and tries an address
  family that did not connect (IPv6 without a route) second for a while. Under load, a download
  through the guard went from about 2 MB/s to some 1.4 GB/s, and a page from 50 sites from 10 s
  to a quarter of a second.
- **Each browser's guard serves that browser alone:** it asks for credentials of the browser's
  own (Proxy-Authorization, which Playwright answers), so a browser that reaches another
  browser's guard, one that took the port of a guard that crashed, say, is refused there.

### Fixed

- The recorder's "Sites see …" check asked Cloudflare through the browser's own request context,
  so Cloudflare's cookie (`__cf_bm`) landed in the recording and in the job's kept session; it
  asks through a request context of its own now, on the browser's way out.
- The next page's link of a pager without classes was its first link, "‹ previous" on page 2:
  a `rel="next"` or the link's text (`›`, *Next*) comes first now.
- A listing reached through the recorded steps kept its address with a sign-in's spent
  `?code=` or `?ticket=`: the job starts where the steps end then.
- Proposed patterns held a session's id (`&sid=abc`) and never matched a link with a fragment
  (`1.pdf#page=1`); volatile and tracking parameters and fragments are no part of them now.
- `Report%202024.pdf` was proposed with the number 202024 instead of the year 2024.
- Links of one script (`download.php?id=N`) were all proposed the file name `download.php`:
  their text or their groups now, with `{disposition}` offered.
- The recorder outlined a pattern as JavaScript reads it: Python's own syntax (`(?P=name)`, a
  leading `(?i)`, `(?x)`) outlined wrongly or not at all.
- Steps after the sign-in that lead to the listing (a tab, *Show all*, a filter) were skipped
  with a kept session, and a username-first or single sign-on page was taken for the listing.
- A listing page that was gone (404), failed (500) or asked to wait (429, 503) failed the whole
  run with httpx's error; now a page below the listing is empty (logged), the listing's own
  error is readable, and pauses are waited for. Listing pages are paced, `<base href>` is
  honoured, the browser's language is sent.
- A run whose deeper pages showed a sign-in, an error or another page counted as a complete
  pass, so known files ended the next runs before those files came.
- Addresses with a session or a signature in them (`;jsessionid=`, `sid`, `X-Amz-Signature`,
  `utm_*`) were new files every run, saved again as "x (2).pdf".
- A next page that loops was read again silently; a next page that is a button ended an HTTP
  job after page one without a word; a link shown on several listings took the first one's
  values; a web page instead of a file always said "signed out?".
- **A password shown as text was recorded:** with a "show password" switch on, the password
  went into the recipe as a value, and the user name stayed one too. A field that was ever a
  password's (or has a password's autocomplete) is one for good. A fill with a value of its own
  in what looks like a password's field is pointed out in the job's form, the recorder and the
  app's log (its step and field, never the value).
- A sign-in submitted with Enter was recorded with the password filled twice and a click on the
  submit button, and failed to replay; a list's choice was a fill (which fails), a checkbox's
  label toggled the box twice, and a file input opened a file chooser: now one fill per field,
  no click of the browser's own, a step `select`, one click, and a message.
- A popup that closed itself left the recorder's screen on the closed tab ("Target closed"),
  and a popup at its opener's origin was not recorded at all.
- The cookies panel let a cookie be set for a public suffix (`co.uk` while at
  `shop.example.co.uk`): such domains are refused, by the public suffix list.

## 0.10.0 - 2026-11-02

### Added

- **On-demand jobs:** a job of a website, feed, video or gallery source may run on demand
  instead of on a schedule: *Fetch a URL…* (or `POST /api/jobs/<id>/fetch`) sends it an address,
  fetched in place of its own for that run alone, its options unchanged. The addresses wait in
  the database, in order and each once, listed with the job and cancelled there; files fetched
  before are not fetched again. A **one-time** job is deleted after a run that went through, its
  files staying. **Send to Harvester**, a bookmarklet, opens Harvester with the page one is on
  (`?fetch=`) to fetch it with an on-demand job or to make one from it. A template, *Video on
  demand*.
- **The queue for everyone:** a line above the jobs says how many runners and browsers are busy
  (browsers by jobs and by recordings) and how many jobs wait, and a waiting job says its place
  and why (a runner, its site, a browser), without naming anyone else's jobs or sites
  (`GET /api/status`). The recorder's "all browsers in use" says what holds them.

### Fixed

- *Test the recipe* stopped by its person during a step said "failed": a stop cut short a step,
  and the step's error took its place.

## 0.9.0 - 2026-11-02

### Added

- **Recorder controls:** Back, Forward and Reload in both browser views; every pick (links to
  follow, file links, next page) can be edited, picked again or removed, with *Undo pick*; the
  recorder opens with the form's recipe.
- **Numbered pages:** picking a page number proposes them; a job finds the current page (marked
  number, the one that is no link, the address, its own count) and takes the next, as a link or
  by clicking a button.
- **Test the recipe:** walks the recipe as a job would, on screen and without downloads (at most
  20 file links, 30 pages, 3 minutes), and lists the pages, the files with their names, and what
  failed.
- **Scroll to load more:** a recorded step for listings that grow as one scrolls.
- **What the browser saw:** a failed step or a challenge keeps a picture of the job's page with
  its address (*Show what the browser saw*, for the job's owner only), from which the page opens
  in the browser; the log names the failing element.
- **Time zones:** a job's schedule runs in its time zone (the browser's; older jobs keep UTC
  until saved), and the schedule's preview shows it.
- **Queued:** a job asked to run shows that it waits for its site or a free runner.
- *Newest releases* for release jobs (the template takes the newest one); a size limit for mail
  and for yt-dlp.

### Changed

- *Save* also stores what was typed into the account's fields; *Save account* says so, and
  errors stay on the form.
- One size unit everywhere: MB of 1000 × 1000 bytes, in the form, the log and the limits.
- *Stop at known files* also finds new files on sites that list oldest first.
- Mail reads each message once (a mark per message), fetches headers in batches and lets the
  server search for a plain sender; only the current attachment is held in memory.
- Changing *Files per run*, file names, tags or the schedule no longer makes the next run go
  through the whole site; only options that change what is listed do.
- A pending tags pass survives restarts and failures; a *Run now* queued before a change of the
  tags does the pass, then the run.

### Fixed

- A file answering 401 or 403 fails alone; the job is refused only when every file of a run is.
- A file refused by its type or size is skipped and logged, not tried again every run (a change
  of the job's type or size limits tries it again).
- A second file of one name is saved as "Name (2)", not taken for the first one.
- An item whose file name cannot be built fails alone, naming the missing value.
- A changed password, token or cookie forgets the job's kept session.
- Buttons whose request the app refuses (Stop as a run ends) say why beside the job; an update of
  the list no longer replaces an open form; the rclone wizard's buttons wait for its steps.
- The recorder's screen stayed empty now and then: a screencast refused while the page is being
  attached is tried again, or started at its next load.
- A browser is always freed: the end of rclone's wizard ends its sign-in (also from a worker
  thread), and a view of the page left without *Cancel* closes its connection.
- A remote's configuration set up anew for a job wins over the tokens kept from the one before;
  the kept configuration names its account.

### Security

- Every connection of a browser goes through its own guard, a proxy in the app: a web worker's
  WebSockets and anything else Playwright's routes do not see reach only public addresses, each
  name looked up once and its checked address connected (no DNS rebinding), also through the home
  connection (whose proxy is asked for addresses only, plain http in a tunnel). WebRTC sends
  nothing past the proxy. rclone's listener is open to the sign-in's browser alone.
- A job's options are checked by their kind: a broken flag could keep a browser's slot until a
  restart.
- The browser source's downloads go through the guarded client, streamed, with the browser's
  cookies; pages and feeds are read up to 16 MB.
- A job's id is never used again (the database is migrated once), and deleting a job stops its
  run.
- GitLab tokens are sent as `Authorization`, which no redirect to another host carries.
- The app cannot be inspected by its helpers (undumpable); frpc runs as a user of its own with
  its own copy of HaRP's TLS files; Playwright's driver gets no secret.
- Names are resolved for every address check; pages' WebSockets and service workers are blocked.
- A person's pattern runs with a time limit, in the recorder and in the runs, without holding the
  other threads.
- gallery-dl's proxy, with its password, goes in its 0600 configuration, not on its command line;
  the rclone wizard leaves no configuration behind when its limit is reached.

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
