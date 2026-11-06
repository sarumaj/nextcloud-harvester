# Harvester: developer's guide

## Layout

- `appinfo/info.xml`: the app's description for AppAPI (its image, its routes).
- `ex_app/lib/main.py`: the app (FastAPI): the jobs' API, the recorder's WebSocket, the page's
  registration; `ex_app/lib/harvester/routes/` the other groups of routes, each a FastAPI
  `APIRouter` that `main.py` includes (Try the address, recipe files and templates, the rclone
  wizard), and what they share (`common.py`: the database, the person's client, the job's checks).
- `ex_app/lib/harvester/`: the jobs' store (`store.py`, SQLite), the executor and its runners
  (`executor.py`, `runner.py`), the sources (`sources/`), recipes and their patterns, the
  browser, the recorder and the guard (`browser/`), the templates (`presets/`), the admin's DNS
  servers through dnsproxy (`dns.py`).
- `ex_app/js/harvester-main.js`: the page in Nextcloud (plain JavaScript, no build).
- `l10n/`: the translations (German `de` and formal `de_DE`, Polish `pl`), `.json` and `.js`
  alike; `scripts/l10n.py` collects the texts and writes the `.js` files (see
  [Translations](#translations)).
- `tests/`: pytest, against fake sites and servers, without network; `tests/server/check.sh`
  the image's check on a real Nextcloud (see [On a real Nextcloud](#on-a-real-nextcloud)).
- `scripts/rclone_backends.py`: the review of a new rclone's remotes (see
  [Sources](user.md#sources)).
- `Dockerfile`, `start.sh`, `healthcheck.sh`: the image (frpc, rclone and dnsproxy pinned with
  their SHA-256 checksums, raised by hand).

## Developing

```bash
python3 -m venv .venv && URLLIB3_NO_OVERRIDE=1 .venv/bin/pip install --no-binary=urllib3-future \
    -r requirements-dev.txt
.venv/bin/playwright install chromium   # for the browser's tests (the full browser)
.venv/bin/python -m pytest -q
.venv/bin/black --check ex_app tests scripts && .venv/bin/pylint ex_app/lib tests scripts
.venv/bin/python scripts/l10n.py                 # every text in every language
```

The tests run against a fake magazine archive (`tests/fake_site.py`), over HTTP and in
Chromium, without network.

`tests/test_readme.py` keeps the README and these guides up with the app: every source and
template has its entry in the user's guide ([Sources](user.md#sources),
[Templates](user.md#templates)), each guide is linked from the README, and lines wrap at 100
characters outside code and tables. CI also checks `appinfo/info.xml` against the app store's schema
(without AppAPI's routes, which the schema does not know) and lints the Markdown
(`npx --yes markdownlint-cli2 "*.md" "docs/**/*.md"`).

### On a real Nextcloud

CI's job `server` checks the image on a real Nextcloud with PostgreSQL, 32 and 35, with the
shared action `nextcloud-app-server` of `dawid/ci-actions`: AppAPI registers the image on a
deploy daemon without Docker (`manual_install`, the container started by the action, no HaRP),
which enables it, disables it and enables it again, and `nextcloud.log` may hold no error of
`harvester`. Then `tests/server/check.sh` tests what people get, where pytest has fakes of
Nextcloud: the page in the top menu and its script, the API through AppAPI's proxy (and refused
without a person), the admin settings' forms, the occ command `harvester:library-reset`, a
website job against the fake site (`tests/fake_site.py`, in a container of its own at an address
of `192.88.99.0/24`, which the guard counts as public) whose run saves its file into the admin's
files over WebDAV, tagged, and the recorder: its Chromium starts behind the guard and opens the
fake site. AppAPI's proxy does not carry WebSockets, so the check reaches the recorder's on the
app's container with AppAPI's headers; HaRP, frpc and `healthcheck.sh` are not part of it. With
Docker, the same as CI (the server at `http://localhost:8080`, user `admin`; `--keep` leaves it
running):

```bash
curl -fsSLO https://git.ciepiela.org/dawid/ci-actions/raw/branch/main/nextcloud-app-server.py
docker build -t harvester:ci .
python3 nextcloud-app-server.py run --app-dir . --nextcloud 32 --exapp-image harvester:ci \
    --check tests/server/check.sh
```

The image (`Dockerfile`, from Nextcloud's app-skeleton-python, with Chromium) is built and
registered by the homelab's `nextcloud` role (`nextcloud_harvester`), as
`local.homelab/harvester:<version>`. `appinfo/info.xml` describes the app; its routes are also
in the role's registration.

A source's options (`sources/base.py`, `Option`) are its form: an option of a `group` is folded
away, one with `shown_when` (another option's key, then its values) is shown only while that
option has one of them, a `choices` option lists its `labels`. An option that replaces older ones
gets its value from them in the source's `upgraded()` (a mail job's `fetch` from its flag
`links`, listed in `retired`): the jobs are read so, by the runs, by `GET /api/jobs/<id>` for
the form and by a recipe file's import (without the retired ones), never rewritten in the
database.

## Destinations

A job's *Save to* (`destinations.py`: its options, rules and checks) picks a `Destination` of
`places.DESTINATIONS`: how its run's `runner.Library` is made (`find`, `put`, `tag`, `untag`,
`existing`, which the runner, `deleted.py` and `unpack.py` use alone), whether its files may carry
Nextcloud's tags or it keeps Harvester's labels itself, and the person's secrets it keeps for a job
(deleted with the job or when its place changes). `files` is `nextcloud.py`, `library` `library.py`
(with its routes, `routes/library.py`), `inbox` `inbox.py`, `vault` `vaultlib.py` over
`davstorage.py` (vault.py's storage on WebDAV; its keys set by `routes/vault.py`), `immich`
`immich.py` (Immich's API; the person's account set by `routes/immich.py`: `GET /api/immich`
gives the address they typed, the admin's, whether a key is kept and whether http is allowed,
never the key; `PUT` tests and keeps `{url, key}`, answering the account's email and name or 422
with why; `POST /api/immich/test`; `DELETE` forgets it). The library's
routes are in [The private library's routes](#the-private-librarys-routes). A job's `options`
carry its place (`save_to`, `vault`, `vault_target`, `vault_verify`, `immich_album`) and
`tag_as` (`nextcloud`, `immich`, `labels`, `none`); a job without
them saves into the person's files with Nextcloud's tags, so API clients that send a job's
options back as they got them (Talk AI) keep its place. Another destination is one more
`Destination` with its `Library`, its form's options in `destinations.OPTIONS` (group `save`)
and what it switches off in `destinations.RULES`. Two calls of the runner serve a server that
files by more than a path: `Library.item(values)` gives the values of the item whose files come
next (Immich's date and album), and `Library.found(file_id)` says that `put` found the content
there already (counted as already there); `put` may raise `Refused` for a file the place keeps
none of (skipped for good; an unpacked ZIP's member left out). The progress log's lines of a
file name where it went: `Library.saved`, `there`, `known` and `tagged` give them (*Uploaded to
Immich: a.jpg (2.1 MB), album “Trip”*, *Saved in your library: …*), the person's files' by default.
Its tests run against fakes of Nextcloud whose files are a folder (`tests/test_destinations.py`),
the vault's files are read by the sister app's code where it is there (`tests/vault_js.mjs`), and
Immich's are against a fake of its API on httpx's `MockTransport` (`tests/test_immich.py`).

## The private library's format

Each person's library (`library.py`, `librarycrypt.py`, `librarykeys.py`) is encrypted with keys
of its own:

- **Key pair.** X25519, made at setup. The public key is stored plain (`library_keys.public`):
  writing (a job, an upload, a move) needs only it, so it works while the library is locked.
  The private key is stored twice, wrapped with AES-256-GCM (its associated data: the person's
  ID and the public key): `by_password`, with a key from the password (scrypt, `n=2^15, r=8,
  p=1`, a random salt, the costs stored beside it), and `by_recovery`, with a key from the
  recovery key (160 random bits, shown once as base32 in groups of four; HKDF-SHA256 with a
  random salt). Unlocked, the private key is kept in memory only (`librarykeys.Session`, by a
  random token), dropped at lock, after the person's minutes without use, and at a restart.
- **Lookup key.** 32 random bytes per person, stored plain: a row's `lookup` is
  HMAC-SHA256(lookup key, `"path\0" + path`), by which a job finds the file it saved while the
  library is locked. It lets whoever has the database test a guessed path, not read one.
- **Per file and folder.** A random 32-byte secret, sealed to the public key (ECIES: an
  ephemeral X25519 key, HKDF-SHA256 over the shared secret with the two public keys as salt,
  AES-256-GCM; associated data `"secret|" + blob ID`). Its subkeys (HKDF-SHA256, info
  `harvester library content|meta|thumb`) encrypt the content, the metadata (JSON: name,
  folder's row, size, type; AES-256-GCM, a random nonce, associated data `"meta|" + blob ID`)
  and the thumbnail (`"thumb|" + blob ID`). A label given or taken off is a row of
  `library_labels`, sealed to the public key (`["+" or "-", label]`, associated data the
  person's ID and the file's row), so that a job labels while the library is locked; they apply
  in order.
- **A blob** on the disk, `library/<person>/<xx>/<blob ID>` (the ID 16 random bytes, in hex):
  `HVL1`, the blob ID, then chunks of 64 KiB each encrypted with AES-256-GCM and its 16-byte tag,
  the nonce the chunk's index (each content key encrypts one blob once), the associated data
  `HVL1`, the blob ID, the index (8 bytes) and whether the chunk is the last. The last chunk is
  padded to 64 KiB: the blob's length gives only the number of chunks; the exact size is in the
  metadata. A range is read by decrypting its chunks alone; a chunk changed, swapped, moved,
  dropped or cut off does not open (`Tampered`), and a blob of another length than its size
  gives is refused. A thumbnail (a JPEG of 320 pixels at most, made with Pillow from a picture of
  32 MB at most when it is written; none for videos) is `<blob ID>.thumb`.
- **Checked when read:** a row whose sealed secret or metadata does not open, whose metadata
  names another folder than its row, or whose lookup is not its path's is left out (logged by
  its row's ID).

### The private library's routes

`routes/library.py`: `GET /api/library/state` (`setup`, `unlocked`, `cookie`: whether the
cookie alone opens it, `minutes`, `usage`, `limit`), `POST /api/library/setup/start` (a recovery
key, kept 30 minutes), `POST /api/library/setup` (`{password, recovery, minutes}`: the key typed
back), `POST /api/library/unlock` (`{password}`), `POST /api/library/recover` (`{recovery,
password}`), `PUT /api/library/password` (`{current, password, minutes}`), `POST
/api/library/lock`. Those that open a session answer its token and set the cookie
`harvester_library` (HttpOnly, SameSite=Strict, Secure unless the page is on http, its path the
proxy's path the page sends as `X-Harvester-Base`); the page keeps the token in memory and sends
it as `X-Harvester-Library` too. AppAPI's proxy (`ExAppProxyController`) passes the browser's
cookies to the app and the answer's headers back, `Range` and `206` included: so read from its
code, not yet seen through a real proxy or HaRP. The page asks the state whether the cookie came
back: if so, an `<img>`, `<video>` or `<iframe>` uses the file's own address; if not, it fetches
pictures and thumbnails with the header into `blob:` addresses, and gets a ticket (`POST
/api/library/files/<id>/ticket`: `?ticket=`, that one file, while the session lasts, an hour at
most) for a video, sound, PDF or download. Unlocked: `GET /api/library?folder=&label=&q=`, `POST
/api/library/folders` (`{path}`), `GET /api/library/files/<id>` (`Range`; `?download=1`), `GET
/api/library/files/<id>/thumbnail`, `PUT /api/library/files/<id>` (`{name, folder}`: rename,
move), `DELETE /api/library/files/<id>`; locked, they answer 403. Set up, locked or not: `POST
/api/library/uploads` (`{folder, name, size}`), `PUT /api/library/uploads/<id>?offset=` (a piece,
16 MB at most, in order), `POST /api/library/uploads/<id>/done`, `DELETE
/api/library/uploads/<id>`.

`routes/moves.py`: the Files action (`nc.ui.files_dropdown_menu.register_ex`, AppAPI's version
2 of file actions, `mime="/"` so that every file and folder has it) posts the selected nodes to
`POST /api/library/from-files`, which proposes a move (`librarymove.py`) and answers
`{"redirect_handler": "harvester"}`: the browser opens the page, with `?fileIds=` or (as
AppAPI does now) without; the page opens the library for either, and for a move not started
that is younger than 15 minutes (its `age`), so a reload shows it too. `GET
/api/library/moves`, `POST /api/library/moves/<id>/start` (`{folder}`). The admin's reset is
the occ command `harvester:library-reset` (`nc.occ_commands.register`), whose handler `POST
/occ/library-reset` is outside `/api`: no proxy route reaches it, and it refuses a request with a
person. Their tests: `tests/test_library.py`, `test_library_crypto.py` (the format, tampering,
50 MB in bounded memory), `test_library_move.py` (a fake WebDAV with a trash bin and versions)
and the page's in `test_page_destinations.py`.

## Trying a job's listing

`POST /api/try` (`tryout.py`, the form's *Try the address*) lists once what a job would fetch,
downloading nothing and marking nothing. Its body is `{"job": <the job as POST /api/jobs takes
it>, "id": <the saved job the form edits, or null>, "account": {<field>: <value as typed>}}`;
the job is checked as Save checks it (422 with why), a website job is refused (its recorder
tests the recipe), and so is an on-demand job without an address. One try of a person's at a
time (409). The executor (`Tries.trial`) runs it in a thread of its own once a runner's place
and the job's site are free (`tryout.CLAIM_WAIT`, 15 s; a try counts among the runners busy in
`GET /api/status`, and a job waits for the place it holds), with the source made as a run makes
it: the guarded client, the proxy, impersonation, the account (the saved job's, a field typed
instead), the kept session (kept again as a run keeps it, for the saved account only). It stops
at `tryout.LIMIT` (60 s; a helper program is killed), or after 2000 files. The answer:

- `ok`, `seconds`, `found`, `cut` (`time`, `most` or empty), `running` (the listing had not
  stopped yet when the answer went);
- `counts`: how many files a run would fetch (`fetch`), leave to later runs (`later`, Files per
  run), has in the job's history (`known`), skips by their type or size (`type`, `size`: by the
  name's extension, by a size the listing gives) or has a problem with (`problem`); `pattern`:
  how many the job's patterns left out (`base.count_misses`);
- `items`: the first 20, each `{path, url, tags, state, pending, problem}` (the path in the
  folder and the tags a run gives it, from the job's templates);
- `outcome` (or `error` and `hint`) and `notes` (the source's progress lines: a mailbox's
  messages), each with its template beside it (`outcome_l10n`, …), which the page translates.
- with `error`, `folders`: a mailbox's folders, the closest to the job's first, when it has not
  the job's (`mail.MissingFolder`; the page offers them for *Folder*), else empty.

## Values of pages and list values

A website's walk (`sources/walk.py`) reads each listing page's values once, before its links:
`Driver.page_info` gives the raw values (`pageinfo.PAGE_INFO`, one `evaluate` in the browser;
`pageinfo.soup_info` over HTTP, from the page parsed already) and `pageinfo.values` makes them
the built-in ones (`page_title`, `site_name`, `page_date` with `page_year`, `page_month` and
`page_day`, `page_lang`) and those of the level's `page_values` (on the first page its link
opened, kept for its next pages). The walk's values are the levels' and the pages', a deeper
page's winning, a link's groups last; the names read from pages never count as a disagreement of
a link's pattern (`Walk.read_names`). The recipe's keys:

```json
{"follow": [{"selector": "a.post", "pattern": "/post/(?P<post>\\d+)$",
  "values": [{"name": "tags", "closest": "div.card", "selector": "ul.chips a", "many": true}],
  "page_values": [{"name": "tags", "selector": "ul.tags a", "many": true, "regex": ""},
                  {"name": "author", "selector": ".byline", "attribute": "", "type": "text"}]}]}
```

`"many": true` makes a value a list (`patterns.page_values`: each entry cleaned by
`patterns.clean_entry`, `MAX_ENTRIES` at most; text or numbers, no dates; a selector required);
`page_values` take no `closest` and never a built-in name, and only levels to follow have them
(`recipe._check_values`). A list value is kept as a list in an item's values (also gallery-dl's
lists of texts and its dicts of them, `sources/gallery.listed`, and yt-dlp's `tags` and
`categories`); `patterns.render` joins it with a comma, `patterns.render_tags` makes a tag per entry
of a template that is the value alone (`MAX_LIST_TAGS`). A source's `tag_values` are offered
under the form's Tags (`/api/sources`).

## Translations

Harvester is translated as Nextcloud's PHP apps are, with `l10n/<language>.json` and
`l10n/<language>.js` (German `de` with *du*, `de_DE` with *Sie*, Polish `pl`). AppAPI copies the
release archive's `l10n/` into the server's apps folder when it installs the app; Nextcloud then
adds the person's `.js` to the app's page and to the admin settings (its `t()` in the browser),
and translates the app's notifications with the `.json`. The app itself (Python) translates
nothing: what it says is English, and Nextcloud or the page puts it into the person's language
where the translations have it. Another language, or a text without a translation, is English.

Translated:

- the page's texts: `t('…')` and `n('…', '…', count)` in `ex_app/js/harvester-main.js`, which
  call Nextcloud's `t()` and `n()` without escaping (the page sets them as text). A text with
  values has placeholders (`t('Delete {name}?', { name })`), never pieces put together, so a
  language can order them; a count is `n()` (`%n` and its forms). Only quoted texts are found:
  no variables, no template strings;
- what the page shows of the server's lists, through the page's `said()`: the sources' titles,
  descriptions and account fields, the options' titles, help and choices' words, the templates'
  titles, descriptions and parameters;
- the admin's settings (Nextcloud's settings page runs `t()` over their titles, help and
  placeholders): no `<…>` in them, which Nextcloud's sanitizing drops;
- the API's errors given as plain strings (`HTTPException(404, "no such job")`), shown by the
  page through `said()`;
- the notifications: `executor.notify(nc, "Harvester: {job} failed", {"job": name}, …)` sends
  the subject and message with their values as rich parameters, and Nextcloud translates them
  for the person who gets them;
- the server's texts with values, as templates (`harvester/l10n.py`): the schedule in words, why
  a job waits (and the recorder's refusal when no browser is free), the progress log's usual
  lines, a run's outcome (a job's last message), a changed site's reason, and the recorder's
  warnings. `said("Saved {path} ({size})", path=…, size=…)` is the English text, a `str` for the
  logs, the database and API clients that do not translate, and carries its template; the API
  gives it beside the English as `<field>_l10n` (`message_l10n` of a log line and of the
  recorder's messages, `last_message_l10n` and `waiting_l10n` of a job, `description_l10n` and
  `error_l10n` of `/api/schedule`, `warnings_l10n` of the recorder's recipe):
  `{"text": "Saved {path} ({size})", "params": {…}}`, a count's
  `{"text": "%n file", "plural": "%n files", "count": 3, "params": {…}}` (`said_n()`), a list
  `{"list": […], "kind": "and"}` (`listed()`, joined on the page with `Intl.ListFormat` in the
  person's language). The page's `rendered()` translates it with `t()` or `n()` and fills in the
  values as text; a value may be a template of its own (a day's name, "next run"). Each text is
  a whole sentence or phrase, a count `said_n()`; a value is never a piece of English made
  elsewhere, except an error. The database keeps the template beside the message
  (`log.message_l10n`, `jobs.last_message_l10n`); a line or message from before has none and is
  shown as it is, in English.

Not translated: the reasons a run fails (the sources' and programs' errors, also inside a
translated line: "Skipped {file}: {error}"), the progress log's rarer lines (a repaired
selector, a source's own), the server's errors with values in them that are not `said()`,
the notifications' values (a changed site's reason in English), recipes and their keys, the
API's identifiers, and the logs (for the admin).

**A new or changed text:** write it in English where it is shown, as above, then

```bash
.venv/bin/python scripts/l10n.py --missing de   # the texts German lacks, as JSON
```

translate them into each `l10n/<language>.json` (a plural's key is `_one_::_many_`, its value
the language's forms: two in German, four in Polish; the form of 1 may leave out `%n` where the
English one does), and run

```bash
.venv/bin/python scripts/l10n.py --write   # orders each .json, writes its .js
```

`tests/test_l10n.py` fails while a language lacks a text, keeps one the app no longer has, has
other placeholders or `%n` than the English, or its `.js` differs from its `.json`; the page's
tests run in English and in German (`@pytest.mark.language("de")` in `tests/test_page.py`). A
new language is a new `l10n/<language>.json` with its `pluralForm` (Nextcloud's, from its
server's `l10n/<language>.json`), filled the same way. German as Nextcloud's: *du* in `de`,
*Sie* in `de_DE`, the same words otherwise; quotes „…“ in German, „…” in Polish.

## Releases

The release bot (`release-bot` on git.ciepiela.org) tags a release weekly when one is due, from
the Conventional Commits since the last tag and the `## Unreleased` entries of `CHANGELOG.md`: it
writes the version and the image tag into `appinfo/info.xml` (`.cz.toml`) and the changelog's
section, commits `chore(release): X.Y.Z` and tags it. A tag `vX.Y.Z` (the version and the image
tag in `appinfo/info.xml`), the bot's or one pushed by hand, runs `.forgejo/workflows/release.yml`:

- **Archive:** `harvester-X.Y.Z.tar.gz`, the source the image is built from, on the Forgejo
  release (signed with `NC_APP_SIGNING_KEY` when set), with the translations (`l10n/`) AppAPI
  installs from it; the release's text is the version's section of `CHANGELOG.md` (a tag
  without one fails before anything is released). The shared action `nextcloud-app-release` of
  `dawid/ci-actions` signs, attaches and writes the text.
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

For the app store, the store's certificate for `harvester` then signs the archive, and the
release can be published there as the other apps are.
