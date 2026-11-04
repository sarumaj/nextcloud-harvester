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
- `tests/`: pytest, against fake sites and servers, without network.
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

The image (`Dockerfile`, from Nextcloud's app-skeleton-python, with Chromium) is built and
registered by the homelab's `nextcloud` role (`nextcloud_harvester`), as
`local.homelab/harvester:<version>`. `appinfo/info.xml` describes the app; its routes are also
in the role's registration.

A source's options (`sources/base.py`, `Option`) are its form: an option of a `group` is folded
away, one with `shown_when` (another option's key, then its values) is shown only while that
option has one of them, a `choices` option lists its `labels`. An option that replaces older ones
gets its value from them in the source's `upgraded()` (a mail job's `fetch` from its flag
`links`): the jobs are read so, by the runs and by `GET /api/jobs/<id>` for the form, never
rewritten in the database.

## Destinations

A job's *Save to* (`destinations.py`: its options, rules and checks) picks a `Destination` of
`places.DESTINATIONS`: how its run's `runner.Library` is made (`find`, `put`, `tag`, `untag`,
`existing`, which the runner, `deleted.py` and `unpack.py` use alone), whether its files may carry
Nextcloud's tags or it keeps Harvester's labels itself, and the person's secrets it keeps for a job
(deleted with the job or when its place changes). `files` is `nextcloud.py`, `library` `library.py`
(with its routes, `routes/library.py`), `inbox` `inbox.py`, `vault` `vaultlib.py` over
`davstorage.py` (vault.py's storage on WebDAV; its keys set by `routes/vault.py`). The library's
routes: `GET /api/library/state` (whether it has a password and is open to this browser, its
minutes, usage and limit), `POST /api/library/unlock` (`{password}`: a token, sent back as the
header `X-Harvester-Library`, or `?unlock=` in a file's address for `<img>` and `<video>`), `POST
/api/library/lock`, `PUT /api/library/password` (`{current, password, minutes}`), `GET
/api/library?folder=&label=`, `GET /api/library/files/<id>` (`Range`; `?download=1`) and `DELETE
/api/library/files/<id>`; locked, they answer 403. A job's `options` carry its place (`save_to`,
`vault`, `vault_target`, `vault_verify`) and `tag_as`; a job without them saves into the person's
files with Nextcloud's tags, so API clients that send a job's options back as they got them (Talk
AI) keep its place. Another destination is one more `Destination` with its `Library`, its form's
options in `destinations.OPTIONS` (group `save`) and what it switches off in `destinations.RULES`.
Its tests run against fakes of Nextcloud whose files are a folder (`tests/test_destinations.py`),
and the vault's files are read by the sister app's code where it is there (`tests/vault_js.mjs`).

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

A tag `vX.Y.Z` (the version and the image tag in `appinfo/info.xml`) runs
`.forgejo/workflows/release.yml`:

- **Archive:** `harvester-X.Y.Z.tar.gz`, the source the image is built from, on the Forgejo
  release (signed with `APP_SIGNING_KEY` when set), with the translations (`l10n/`) AppAPI
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
