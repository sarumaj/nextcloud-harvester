# Harvester: user's guide

What a person does with Harvester: jobs and their sources, the recipes of websites and the
recorder that makes them, and what a run does. Installing and the admin's settings are in the
[administrator's guide](admin.md), the code and the tests in the [developer's guide](developer.md).

## Jobs

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
- **Notifications:** a job that needs a person, fails, or finds its site changed notifies its
  user; the same failure (or change) again on the next runs does not. A one-time job tells what
  it did as it is deleted.
- **Language:** the page and the notifications are in your language in Nextcloud (*Personal
  settings*), in German or Polish; in another language they are English. So are the schedule in
  words, why a job waits, the progress log's usual lines and a run's outcome, and the recorder's
  warnings. English stay the reasons a run fails (a site's or a program's errors), the log's
  lines from before this version, a site's own texts and the recipes.

## Sources

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
    `max_filesize`). *Impersonate a browser* has yt-dlp present itself as a browser (its own
    impersonation with `curl_cffi`, for sites behind Cloudflare that answer it with 403);
    extractors that need it use it anyway. For galleries, the same option is gallery-dl's browser
    emulation (Chrome's headers and TLS ciphers), and Harvester's own downloads of its files
    impersonate a browser as the
    [administrator's guide](admin.md#bot-checks-the-browser-disguise-and-impersonation) describes.
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

## Templates

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
  - `save-page`: an on-demand job that saves the pages you send it (*Fetch a URL…*, *Send to
    Harvester*) as files named after their titles: PDF, HTML or Markdown (its parameter *Saved
    as*), for receipts, order confirmations and articles that exist only as a web page;
  - `gallery`: a gallery or album's images;
  - `nextcloud-share`: the folder of another Nextcloud's public link;
  - `app-releases`: an application's release downloads, of its newest release.

## Accounts

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
  - **a mailbox for codes** (`code_mailbox`, websites): the IMAP mailbox a sign-in's code comes
    to, `key = value` per line: `host`, `user`, `password`; `port`, `folder`, and `auth` with
    OAuth2's `client_id`, `client_secret`, `refresh_token` (and `token_url`) as for mail. Without
    a user and password of its own, the job's username and password sign in to it.

## Recipes

- **Recipes** (`ex_app/lib/harvester/recipe.py`): the steps to sign in (replayed in Chromium;
  without steps the pages are fetched over HTTP), the listing to start at, levels of links to
  follow (a year, then its issues), the file links, and the next page's link. Each level has
  a selector and a pattern: a regular expression on the link's URL whose named groups
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
  session the sign-in is skipped but the steps after it still run. Ordinary
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
- **Pauses and retries:** a step with `"pause": 5` waits that many seconds (above 0, at most 60)
  before it, for a page that needs a moment after the step before. A step with `"retry":
  {"times": 3, "delay": 2}` is tried again up to 3 more times (1 to 10), 2 seconds apart (0 to
  60, 2 without), when it fails as a step fails: its element or frame not there in time, its
  popup not opened, its wait not met. After its last try it fails as before (an optional step is
  skipped). A stop ends a pause or a delay at once and is never tried again. The run's log says
  `step 3 (click “Search”): pausing 5 s before it` and `step 3 (click “Search”) failed, trying
  again in 2 s (attempt 2 of 4): <why>`. Recipe files keep both; the recorder's Steps list edits
  them (**pause, retry**).
- **Selectors with alternatives** (`harvester/selectors.py`): recorder.js finds a step's element
  by every selector that matched it alone when it was recorded, ranked as Playwright's codegen
  ranks them: a test id (`data-testid`, `data-test`, `data-qa`, `data-cy`), an id that does not
  look generated (`:r1:`, `ember123`, hashes), its role and accessible name
  (`internal:role=button[name="Sign in"s]`), `aria-label`, `name`, stable classes, its text
  (`span:text-is("Accept all")`), its place among its siblings last; within the nearest
  ancestor with a test id or id when that makes one unique (`#login input[name="user"]`). A
  role is only written where its name is certain (no pictures, icons from CSS or hidden text
  inside). The step keeps the best as `"selector"`, the rest as `"alternatives"`. A replay
  (`selectors.resolve(page_or_frame, step)`) takes the selector once it finds anything; after
  2 s without, the first alternative that finds exactly one element, within the step's time,
  and the log says `step 3: selector repaired (#old → [data-testid="go"])`. Levels and the file
  links keep alternatives that matched the same links (within a stable ancestor, the links'
  own classes, their file type: `a[href$=".pdf" i]`), tried when the selector's links match
  none of the pattern (`level 1: selector repaired (…)`, `file links: …`); the next page's link
  its own, tried when it is not on the page (`next page: …`). The recipe stays as it is, each
  repair is said once a run and **Test the recipe** lists them. Over HTTP the role selectors of
  links and buttons and `text-is` work too. Recipes without alternatives run as before. For
  the next page's link a `rel="next"` comes first, and its place only when nothing else finds
  it: a pager without classes (`‹ 1 2 ›`) is found by its `›`, never as its first link, which
  on page 2 is "‹ previous".
- **The listing where the steps end:** a listing picked on the page the recorded steps lead to
  (nothing browsed since), whose address carries a token of the visit (`code`, `ticket`,
  `token`, `sid`, `session`, `state`, `sig`, `signature`, `expires`, `X-Amz-*`, …), is no
  address in the recipe: the job starts where its steps end. The recorder warns of such tokens
  in the listing's address and in a step's (an address typed, Back or Forward to a sign-in's
  return address), as they are likely spent when the job runs.
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
  the pattern's groups and `url`, `text`, `page`, `host`, `filename`, `stem`, `ext`, and in the
  file's name and tags `{disposition}`: the name the site gives the file as it sends it
  (`Content-Disposition`, `filename*` before `filename`; else the last part of the address it
  came from), without folders, control characters or leading dots, 200 characters at most. Such a
  file is downloaded first and then put where its name says; what tells it apart from the next
  run's files is still its link.
- **More of a recipe:** a fill step's value may hold dates, filled in at each run in the job's
  time zone: `{today}` (`2026-11-02`), `{today-30d:%d.%m.%Y}`, `{today+1w}`, `{today-1m}`,
  `{year}`, `{year-1}`, `{month:02d}`, `{day}`; other braces are typed as they are. The file
  links may have `"key": "{year}-{issue}"`, a template of their values that tells files apart
  instead of their address (for addresses that change), and `"click": true` for elements that
  give the file when clicked (a button, a form, a script, a blob; no address): the job clicks
  each in the browser, its pattern searched in the element's text (an icon's `aria-label` or
  `title` where it has none), and keeps what the click gives: the browser's download (also one a
  tab or popup it opened starts, or the page it opened starts a moment later), or the file a tab
  or popup it opened shows (a PDF in the viewer, a picture), or the file it took its own tab to;
  a file a tab shows is fetched by its address with the browser's cookies, as other file links
  are. Each is checked as any download (its types, its smallest and largest size, before
  anything is copied; never a web page: a click that only leads to one fails its file after ten
  seconds); the tabs it opened close and its own tab goes back to the listing. `{disposition}`
  is the name the file came with, and such a file is told apart by its listing and its text
  unless the recipe has a key. Each level (an entry of `follow`, or
  `links`) may load more of its pages before they are read: `"scroll": 20` (rounds, as the
  scroll step) and `"load_more": "button.more"`, clicked until it is gone or the page stops
  growing (50 times or a minute at most). These need the browser, as does a next page that is a
  button (`"buttons": true`): over HTTP such a page says so instead of ending after page one.
  A recipe is checked when the job is saved: the kinds of its parts (text, numbers, lists), the
  actions, credentials (`username`, `password`, `totp`) and keys of its steps (`Enter`, `Tab`,
  `Control+A`, one character, ...), its dates, the next page's level, the key's values.
- **Levels that are clicked** (`harvester/sources/clicks.py`): a level of links to follow may
  have `"click": true` for elements without an address that open an entry (a row, a card, a
  button): `{"selector": "tr.invoice", "pattern": "^Invoice (?P<number>\\d+)", "click": true}`.
  Its pattern is searched in each element's text (as a clicked file link's), whose groups are
  values for the levels below; the elements are told apart by their text and their place. The
  job clicks each in turn, takes where it lands (its tab at another address, a new tab or
  popup, or the page changed in place: a single-page app's route, a dialog), waits for the
  network to go quiet and for the next level's elements there (five seconds at most), reads the
  next level, and returns to the listing: the new tab closed; else **Back** (after an address)
  or **Escape** (after a change in place) while that shows the listing as it was (its elements,
  their texts, no more of the next level's); else the listing opened again at its address with
  its level's `scroll` and `load_more` done again. Then the next element is found by its place,
  else by its text. A click that changes nothing within the step's time, or whose element is
  gone, is a gap of the run (`level 1: clicking “Invoice 7” failed: …` in the log), and the
  walk goes on. The limits: a listing opened again starts at its address, so pages reached by
  buttons without an address (`"buttons": true`) and steps after its address are not done
  again; an element needs a text (or an `aria-label`, a `title`).
- **Values from the page** (`patterns.page_values`): a level of links to follow or the file
  links may take `"values"` from the page around each link, not only from its address: the
  link's text, an attribute, or an element near it (`{"name": "date", "closest": "tr",
  "selector": "td.date", "attribute": "", "regex": "...", "type": "date"}`: the link's row or
  card, the element in it, its attribute or else its text, narrowed by a pattern whose named
  groups are values too). A value is `text`, a `number` or a `date`: dates as English, German and
  Polish pages write them (`2026-11-02`, `02.11.2026`, `2. November 2026`, `2 listopada 2026`,
  `November 2, 2026`, `listopad 2026`; numbers day first) become `2026-11-02`, with `{year}`,
  `{month}` and `{day}` where the address's pattern gives none. They name files and tags as the
  pattern's groups do; a value its element or pattern does not find is missing (a file name
  naming it fails that file, a tag naming it is left out). Read alike over HTTP and in the
  browser; a level without values reads its links as before.
- **Listings in JSON** (`harvester/jsonlist.py`): the file links may be `"kind": "json"`, for a
  site whose page fetches its listing (an XHR): the request's address (`{year}` and the like from
  the levels before), the path to its items (`data.items`: JSONPath's dotted form, with `[n]` and
  `*`), an item's fields for the file's address and its text, values from fields
  (`{"name": "date", "field": "issued", "type": "date"}`), and a page parameter (`{"param":
  "page", "first": 1, "step": 1}`: a page number, or an offset) set for each page until one has
  no items, or the job's *Pages*. The job asks for it as for a file: the guarded HTTP client,
  with the browser's cookies when it has a browser (not the browser's own `context.request`,
  which passes by its routes); at most 16 MB, 64 levels deep and 1000 items a page.
- **Pages saved as files** (`harvester/sources/keep.py`): for what exists only as a web page (a
  receipt, an order's confirmation, a payslip, an article), the file links may have `"save": "pdf"`,
  `"html"` or `"markdown"`: each page a link leads to (its address, or what an element that is
  clicked shows) is opened in the job's browser, settled (the network quiet, scrolled to its end so
  that lazy pictures load: the level's `"scroll"` rounds, else 3) and saved, not downloaded. `pdf`
  is Chromium's print (A4, with backgrounds); `html` one file with the page's stylesheets, pictures,
  fonts and frames inside: Chromium's snapshot (MHTML, as *Save page as* makes it: the page as it
  shows it after its scripts, without them), each resource turned into a `data:` address
  (`harvester/mhtml.py`), since MHTML opens only in Chromium's browsers and Nextcloud shows none.
  Nothing in it runs: no event handlers (`onclick`, `onerror`, …), no `javascript:` addresses, and a
  policy `script-src 'none'` in its head; `markdown` the main content as Readability finds it
  (paragraphs scored for their containers, menus, sidebars, forms and hidden parts left out) with
  its headings, lists, tables, links and pictures (`harvester/pagetext.py`, on BeautifulSoup: no
  other dependency), under the title and the address. The file's name may hold `{title}`, the page's
  title (a stand-in, the link's text, until it is saved; `{disposition}` is the title with the
  extension), and gets the extension where it lacks it. Pages are told apart by their address or the
  recipe's key, as files are. A page that shows a sign-in is a web page instead of the file (*signed
  out?*), one that answers an error fails alone; a saved page passes the size checks (200 MB at
  most) and the job's file types, which must take what is saved (`any`, or `pdf`, `html` or
  `markdown`; else the job is not saved). A recipe's own `"save"`, beside its `start` and without
  levels or file links (`{"start": "https://…", "save": "pdf"}`), saves the start page itself, where
  the steps end: the template `save-page` is such an on-demand job, whose start is each page sent to
  it.
- **Files a page only shows:** the file links may have `"capture": ["application/pdf"]` (media
  types, `image/*` for any picture, at most 10; never `text/html`): each page a link leads to is
  opened and the job keeps the files of those types that it loads (its own requests, its frames'
  and popups'; a PDF a viewer fetches), those it embeds without loading them (`<embed>`,
  `<object>`, a frame's address: a headless browser shows no PDF viewer, so they are fetched with
  the browser's cookies), and its `blob:` addresses (read in the page). At most 20 files a page,
  each at most 64 MB and the job's largest file, each checked as a download (its types and
  sizes); `{filename}`, `{stem}`, `{ext}` and `{disposition}` are the file's, `{title}` the
  page's, the link's other values stay. A file is told apart by the page's key and its address
  (a blob's by its content); a page whose files were all placed is marked done and not opened
  again (one that gave none is, next run). `save` and `capture` exclude each other, and both
  have the job use the browser (without it, a page could not be rendered: the job says so).
- **Pictures at full size:** file links may be pictures (`img`, `picture`): a picture's address
  is its largest candidate of `srcset` and its `<picture>`'s `<source>`s (by width, else by
  density), before it an attribute that names the full size (`data-full`, `data-zoom-image`,
  `data-large-file`, …) and after it the lazy-loading ones (`data-src`, `data-srcset`,
  `data-original`, `data-lazy-src`, …), its `src` last unless it is a placeholder (a `data:`
  address, `blank.gif`, `spacer.png`, `1x1.gif`, a name with `placeholder`); its text is its
  `alt` (`harvester/images.py`). Alike over HTTP and in the browser, in the recorder too.
- **Codes by e-mail:** a step `{"action": "mail-code", "selector": ..., "from": ..., "subject":
  ...}` types a code the site sends by e-mail. The job waits (`"wait"`: 180 s by default, 10 to
  600) for a message of that sender and subject (patterns, empty: any) that came into the
  account's **Mailbox for codes** after the step began (with a minute's grace: the click before
  it sent the message while its page settled), newest first, and types its code: the step's
  `regex` (its group `code`, else its first group), else the first number of 6 to 8 digits, in
  the subject, the text or the HTML's text. Read-only (the folder selected read-only,
  `BODY.PEEK`): nothing is marked; the code is never logged.

## The recorder

- **Recorder** (`ex_app/lib/harvester/browser/`): a headless Chromium in the app, shown on the
  page and driven from it over a WebSocket. Sign in and go to the listing (recorded as steps),
  browse without recording, then pick the links to follow and the file links: the app proposes
  a pattern with named groups for each (`patterns.propose`), outlines what it matches in the
  page (solid) and what it leaves out (dashed), lists examples with their values, and proposes
  a file name and tags. A proposal leaves out what changes from visit to visit: a fragment
  (`#page=2`), a token's parameters (above) and tracking ones (`utm_*`, `;jsessionid=`); the
  pattern takes any or none of them. It reads `Report%202024.pdf` as `Report%20` and the year
  2024. Links that share a generic name (`download.php?id=N`) are named after their texts
  (`{text}`), or their groups (`{id}`) when the texts are the same, and the recorder offers
  `{disposition}`, the server's name for the file. Edit a pattern and the outlines follow, as
  Python reads it: JavaScript's RegExp outlines with Python's syntax translated (`(?P<n>`,
  `(?P=n)`, a leading `(?i)`), and with Python's own matches where it cannot (`(?x)`, `(?i:…)`,
  atomic groups). Passwords are never recorded: the job fills in its account. The **Steps**
  list edits the steps: **↑** and **↓** move one, **✕** removes one, a fill fills in a value of
  its own (edited there), the account's user name, password or the current one-time code, a
  list's choice can be edited, a step's wait dropped (**wait for the network instead**), and its
  pause and retries set under **pause, retry** (while recording too, the last step's). A
  recipe with steps opens in **Browse**; **Record steps** then asks where the new ones go: after
  a step (the last, unless chosen), keeping the later ones (**Append**), instead of them
  (**Replace from there**), or instead of all (**Replace all**). A fill with a value of its own
  in what looks like a password's field (`#pw`, `[type=password]`, a name with `pass` or `pwd`),
  from an earlier recording, is pointed out in the job's form, in the recorder and in the app's
  log when the job is saved, by its step and field, never its value. **Back**, **Forward**,
  **Reload** and the address field's **Go** are buttons (Tab and Enter reach them), here and in
  **Continue in the browser**. They
  are steps only while recording: an address typed, Back and Forward become a step "goto" to the
  page arrived at (a job's browser has no history to go back in), Reload none; browsing and
  picking record nothing. Every pick (each level of links to follow, the file links, the next
  page) has the same controls: its element (a selector, its alternatives listed) and pattern to
  edit (a selector edited drops its alternatives), **Pick again**
  and **Remove** (a removed level's later levels move up); **Undo pick** takes back the last
  change of the picks. The recorder starts with the form's recipe, so its picks can be changed
  there. Elements without an address (a button, a link to `javascript:` or `#`, a row or card a
  script opens: the outermost element under the hand cursor) picked as file links or as links to
  follow make a pick that is clicked (`"click": true`): its pattern is proposed on their texts,
  their common words around a value `label` (`^Invoice\ (?P<label>.+)`, else `.`, any text), its
  examples are their texts, the file links' name is `{disposition}`, and the panel says that the
  job clicks them in the browser; **Click in the browser** switches a pick between the two (its
  pattern proposed anew from the page). Below a level that is clicked, a page at another address
  than the one it was picked on is its entry's (its file links are picked there); a dialog or
  panel on the listing's own address is not, so pick the file links in it, not another level.
  **Test the recipe** clicks such levels as a run does; it lists file links that are clicked by
  their text (`(clicked: “Get A”)`) without clicking them, and as the job runs it probes none.
  **Save the pages as** (the file links' pick: *Files*, *PDF*, *HTML*, *Markdown*) saves the
  pages they lead to instead of downloading them (`"save"`) and proposes `{title}` as the file
  name; **Test the recipe** lists those pages (`(the page saved as PDF)`) and a recipe's
  `capture` (`(its files of the types image/*)`) without opening them, and probes none. A picture
  picked as a file link is taken at its full size, its examples show that address.
  Picking a number of a numbered pagination as the next page proposes numbered pages, showing
  this page's number and the next one's (**Use the picked link alone** switches back).
  **Add: scroll to load more** (while recording) adds that step and scrolls the page.
  **Pick a value** (with each pick of links), then a click on a text near one of its links (a
  date in its row, a title in its card), adds a value: where it is from the link, and a type and
  pattern proposed from what it reads beside every link (a date's form recognised); the examples
  show the values, and their names, types and patterns can be changed. **Network** lists the
  page's answers in JSON to its XHR and fetch() requests (address, size); **Preview** shows one,
  indented (its first 20 000 characters; one larger than 2 MB is not kept); **Use as the file
  links** makes one the JSON listing, its items, fields, date and page parameter proposed from
  the answer, to check and change. In the Steps list a code typed into a field offers **code from
  e-mail?** (its sender and subject), which makes it the step above. The window's size the steps
  were recorded and the links picked in is the recipe's `"viewport"`; the job's browser takes it
  (1280 × 800 without one).
  **Test the recipe** runs it as a job would (the site source's own walk, `Site.survey`, in a
  browser context of its own with the recorder's session, shown on the screen; nothing recorded,
  nothing downloaded) and lists the pages it read with the links matched, the first 20 file
  links with the names the job's *File name* gives them, and the problems: a step that failed
  (with the page it was on), a selector that matched nothing, a pattern none matched. It reads
  at most 30 listing pages and three minutes; **Stop the test** ends it, and its context closes.
  Meanwhile the Steps list follows it: the step playing is outlined (`▶ playing`, `attempt 2 of
  3`, `⏸ pausing 4 s` counting down), each step done marked `✓ passed`, `– skipped (optional)`,
  `✗ failed` or `↷ not played (signed in)` (the recorder's session skips the sign-in). A failed
  step stays outlined; a click on it opens its **pause, retry** to give it time or tries.
  **Test as the job runs** (a saved job) is the honest one: a new browser context at the
  recipe's window, without the recorder's session, signed in by the steps with the job's stored
  account (its one-time code, its mailbox for codes, its cookies; the app reads it, the page never
  has it), so a sign-in that only the recorder's session got past fails here as in a run; with
  the form's file types and file names (their extension); and the first 64 KB of the first file
  fetched, to tell a file of the job's types from a web page (a sign-in page, say).
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
- **Which address sites see:** the recorder shows it under its address bar ("Sites see
  203.0.113.10 (DE), through the home connection", or "directly, from the server"), and a run
  through the proxy starts its log with it. Both ask Cloudflare's `cdn-cgi/trace` through the
  same connection the pages and downloads use, so it is the proxy's address only when the proxy
  is really used.

## Recipes as files

- **Recipes as files** (`ex_app/lib/harvester/recipe_file.py`): **Export recipe** in a job's form
  downloads `<name>.harvester.json`: `{"format": "harvester-recipe", "version": 1}` with the
  job's name, source, schedule (`""`: on demand), one-time, tag templates and options (a website's
  recipe as an object), checked as *Save* checks the form. Never in it: the account (passwords,
  tokens, cookies, rclone's configuration; a step that fills in the account loses any value), the
  kept session, the folder and the time zone. **Import a recipe** on the *New job* page reads such
  a file (256 KB at most): its schema (every field known and of its kind and size, the format's
  version, a known source, only the source's options), then the checks a saved job passes; the
  form opens filled in, without a folder, to choose the folder and the account; nothing is saved
  until *Save*. Nothing in a file runs. **Save as template** keeps the form's job as such a file,
  the person's own (50 at most, in the app's database): *Your templates* on the *New job* page
  lists them, each to use (checked as an import) or **Remove**.

## When a site changes or asks for a person

- **The site changed** (`ex_app/lib/harvester/watch.py`): each run of a website job counts, per
  level of its recipe (the listing, each level of links to follow, the file links), the pages read,
  the links its selector found and those its pattern matched, and keeps that summary (the last
  five good runs', and five of the others). When a level that found links in the good runs before
  (with the same selector) finds none now, or its links no longer match its pattern, the run ends
  **Changed** (neither OK nor failed): the job list says which level and how many it found before
  (*level 1 found nothing (it found 12 before)*), the progress log adds its selector and the first
  page without a match, with **Show what the browser saw** of that page, and the person is told
  once, until a run is OK again. A listing that never found anything is no change, nor are a page
  asking for a person, a refused sign-in or an on-demand job's fetch (another page). Another
  recipe forgets the summaries. A changed job's row offers **Test the recipe**
  (`POST /api/jobs/<id>/test`): the recipe's test (`Site.survey`, nothing downloaded) as a job's
  turn, in the queue as a run (a runner, its site, a browser), its pages and problems in the
  progress log, judged as a run; a test that finds the site as before ends the warning. A test
  is no run: the schedule counts from the last run. **Test the recipe weekly** (a website job's
  option, off by default) tests it so on Sundays, at an hour of the job's own (counted from when
  it is turned on); a run asked for meanwhile takes the test's place.
- **Sessions and challenges:** a job keeps its browser session (cookies, local storage; encrypted in
  Nextcloud, like its account) and replays the sign-in only when the listing shows a sign-in (a
  password or user name field, or a sign-in's address). The sign-in is the steps up to the last fill
  of a credential and the click or key that sends it; the steps after it (a tab, *Show all*, a
  filter, scrolling) lead to the listing and run every time. When the kept session does not reach
  the listing (a step after the sign-in fails, or the start page has none of the recipe's links: a
  username-first or single sign-on page taken for the listing), the run signs in once anew from no
  session, in a browser of its own, and keeps the new session; if that fails too, the old one is
  forgotten. A recipe with other steps or another site to start at forgets it when saved (other
  links or patterns do not: it may be a person's answer to a challenge). A web page that comes
  instead of a file after files came (the session ended in the middle of the run) signs in anew
  once, and the file is asked for again. When a site asks for a person instead (reCAPTCHA, hCaptcha,
  Cloudflare, "verify you are human") the run stops, the job shows **Needs you** and its user gets a
  notification; scheduled runs wait. **Continue in the browser** opens the recorder's browser with
  the job's session at that page: the person answers it, **Done** keeps the session and runs the
  job. Harvester never answers a challenge itself. When a step fails or a page asks for a person,
  the job keeps what its browser showed (a JPEG of the window, at most 400 KB, with the page's
  address and title; in the app's database, the newest per job, deleted with the job): its line in
  the progress log gets **Show what the browser saw** (`GET /api/jobs/<id>/shot`, the job's owner
  only), and from there **Open in the browser** opens the job's browser with its session at that
  page. A failed step's line names its element when it has no text (`step 2 (click
  a.overlay-b:nth-of-type(6)) failed`). A new value for any of the job's account forgets its kept
  session (the cookies, a refresh token the provider replaced): those are the old account's, and
  would be used instead.

## Runs

- **Downloads:** a pause between files, and one of a second between listing pages; waits when
  the site asks (429 or 503 with `Retry-After`, for listing pages too, or a wait pattern on the
  URL it redirects to, like heise's `wait_sec=`) and three retries of a broken transfer; the
  listing a file is linked from as `Referer`, and the browser's language (`Accept-Language`). A
  web page instead of a file is a sign-in (*signed out?*), or else a page standing in for the
  file (*Your download starts...*), whose meta refresh, or its one link that reads like the
  download (its text, a `download` attribute, a file's extension), is followed once; any other
  page says that the download URL gives a page (a level of links short, or another Download
  URL). Expected file types
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
- **Runs that meet trouble:** a link's address is known without what changes from visit to
  visit (`patterns.VOLATILE`: a session in the path or query, `;jsessionid=`, `sid`, `PHPSESSID`;
  a presigned address's signature and expiry, S3's `X-Amz-*`, Google's `X-Goog-*`, CloudFront's
  `Expires`, `Signature`, `Key-Pair-Id`; trackers, `utm_*`, `fbclid`, `gclid`; the fragment), so
  those make no new files; a key recorded before still counts. A page below the listing that is
  gone or refused (404, 500, 403) is read as empty, the listing itself failing the run (401 and
  403: *Sign-in refused*). Such a page, or one that shows a sign-in or ended up at another
  address, is a line in the log, and the run does not count as a complete pass (known files do
  not end the next runs before it went through). A next page read already at its level (a last
  page linking to itself, or back to the first) ends its listing, with a line in the log. A link
  shown on another listing's page whose values disagree with the levels above it (each year's
  page showing the newest issue) is taken on its own listing, with its own values. After files
  came, ten failures in a row end the run; the rest comes next run.
- **Pause and stop:** while a job runs, **Run now** becomes **Pause** (then **Resume**) and
  **Stop**. Both act before the next file: a paused run waits (its time does not count against
  the two hours; after six hours unresumed it stops), a stopped one ends and keeps what it saved,
  and its next run goes on from there; a stop also ends a wait (a retry, the site asking to wait)
  and a download in progress at once (its chunks, yt-dlp's, gallery-dl's and rclone's: a helper
  program is killed), and the file it was at comes next run.
