# Harvester

The Nextcloud External App `harvester` (AppAPI, Python): jobs that fetch files from websites,
mailboxes, feeds, video and gallery sites, file stores and release pages into a person's folders
on a schedule, and tag them. A website is described by a recipe, recorded in the app's browser
(Chromium, through Playwright) or taken from a template.

## Layout

- `appinfo/info.xml`: the app for AppAPI (version, image, routes).
- `ex_app/lib/main.py`: the app (FastAPI, nc_py_api): the jobs' API and the recorder's WebSocket;
  `ex_app/lib/harvester/routes/`: the other groups of routes (FastAPI's `APIRouter`), with what
  they share (`common.py`).
- `ex_app/lib/harvester/`: the store (SQLite), the executor and runners, `sources/`, the recipes
  and patterns, `browser/` (the recorder, the guard), `presets/` (the templates).
- `ex_app/js/harvester-main.js`: the page in Nextcloud (plain JavaScript, no build).
- `tests/`: pytest against fake sites and servers, without network; `tests/server/check.sh`: the
  image on a real Nextcloud (CI's job `server`).
- `docs/user.md`, `docs/admin.md`, `docs/developer.md`: the guides the README links.
- `Dockerfile`, `start.sh`, `healthcheck.sh`: the image (`start.sh` and `healthcheck.sh` from
  Nextcloud's app-skeleton-python).

## Checks

Run what CI runs for what you changed, and look at each exit code:

```bash
.venv/bin/python -m pytest -q          # the browser's tests need `.venv/bin/playwright install chromium`
.venv/bin/black --check ex_app tests
.venv/bin/pylint ex_app/lib tests
node --check ex_app/js/harvester-main.js && node --check ex_app/lib/harvester/browser/recorder.js && node --check ex_app/lib/harvester/browser/consent.js
shellcheck start.sh healthcheck.sh tests/server/check.sh
npx --yes markdownlint-cli2 "*.md" "docs/**/*.md"
```

CI's job `server` builds the image and registers it with AppAPI on a real Nextcloud (32 and 35),
then runs `tests/server/check.sh` (the page, the settings, a website job saving over WebDAV, the
recorder's browser): for a change of the app, its image or that script, run it with Docker as
`docs/developer.md` says (some two minutes per version).

CI also checks `appinfo/info.xml` against `https://apps.nextcloud.com/schema/apps/info.xsd` with
`xmllint --noout --schema`, without `<routes>` (AppAPI's, which the schema does not know). Tests
that need loopback connections (the guard, the browsers) fail or skip in a sandbox that blocks
loopback; CI runs them all.

## Conventions

- Commit messages: one line, no trailers, as Conventional Commits with the area as scope
  (`feat(vault): …`, `fix(inbox): …`, `docs: …`, `chore: …`), which the release bot reads. A change
  comes with its README or `docs/` text (the README and the guides are updated with every change;
  `tests/test_readme.py` requires each source and template in `docs/user.md` and lines of at most
  100 characters) and an entry under `## Unreleased` in `CHANGELOG.md` (Keep a Changelog:
  `### Added`, `### Changed`, `### Fixed`).
- Markdown: lines of at most 100 characters (tables excepted), tables aligned.
- Python formatted by black (120 characters), pylint at 10; JavaScript indented with tabs
  (`.editorconfig`).
- Every source file starts with `SPDX-License-Identifier: AGPL-3.0-or-later` (after a shebang).
- Safety first: the browsers and downloads reach only public addresses (the guard, the routes'
  checks); never log a password, token, cookie value or one-time code.

## Releasing

The release bot (`release-bot` on git.ciepiela.org, weekly) releases what is due, from the
Conventional Commits since the last tag (`feat` a minor, `fix`, `perf`, `refactor` and `fix(deps)` a
patch, `!` a major) and the `## Unreleased` entries (an `### Added` makes it at least a minor). It
moves the entries under `## X.Y.Z - YYYY-MM-DD` (adding Renovate's updates as `### Dependencies`),
sets `<version>` and `<image-tag>` in `appinfo/info.xml` (`.cz.toml`), commits
`chore(release): X.Y.Z`, tags `vX.Y.Z` signed and pushes; `.forgejo/workflows/release.yml` then
releases it.

To release at once by hand: move the entries, set the version, commit `chore(release): X.Y.Z`, tag
it annotated and signed as the earlier tags (`git tag -s vX.Y.Z -m "Harvester X.Y.Z"`) and push the
tag.

## Screenshots

`screenshots/1-jobs.png` to `4-recorder.png` are the homelab guide's shots
(`docs/assets/screenshots/harvester-*.png` in `dawid/HomeLab`), recorded against a demo
Nextcloud with demo people and data only, scaled down to 1600 pixels wide
(`vips resize in.png out.png[compression=9,palette,strip] 0.625`); the recorder's shows the
server's address, blurred. `appinfo/info.xml` links them by their raw URLs on `main`. Never show
real accounts, addresses or host names.
