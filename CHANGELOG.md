# Changelog

## Unreleased

### Added

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
- Cookie banners dismissed in the progress log: an info line on the page where it happened,
  naming the consent manager (or *a cookie banner*) and the button clicked, once a manager in a
  run.

### Changed

- An address sent to an on-demand job again brings back its files deleted in Nextcloud since,
  whatever the option, and the log says where each file fetched before is now (*Fetched before,
  in your files at …*).
- A deleted file's record of its content (and the duplicates' that named it) goes when the file
  is found gone, so that its download anew is no duplicate of itself; a known file due for its
  check of changes that is gone is downloaded anew instead of staying away.

### Fixed

- Gallery jobs (again): urllib3-future, which nc_py_api's niquests brings, is built without
  installing itself as `urllib3` (`URLLIB3_NO_OVERRIDE`), so gallery-dl gets upstream urllib3 again.
  0.13.1's urllib3 2.7.0 did not help: the fork stood in its place, and lacks
  `urllib3.connection.create_urllib3_context`.
- *Test the recipe* in the recorder failed at a recorded click on a cookie banner that **Dismiss
  cookie banners** had answered already; it skips the click as optional, as a run does.

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
