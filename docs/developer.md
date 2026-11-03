# Harvester: developer's guide

## Layout

- `appinfo/info.xml`: the app's description for AppAPI (its image, its routes).
- `ex_app/lib/main.py`: the app (FastAPI): its API, the recorder's WebSocket, the page's
  registration.
- `ex_app/lib/harvester/`: the jobs' store (`store.py`, SQLite), the executor and its runners
  (`executor.py`, `runner.py`), the sources (`sources/`), recipes and their patterns, the
  browser, the recorder and the guard (`browser/`), the templates (`presets/`).
- `ex_app/js/harvester-main.js`: the page in Nextcloud (plain JavaScript, no build).
- `tests/`: pytest, against fake sites and servers, without network.
- `scripts/rclone_backends.py`: the review of a new rclone's remotes (see
  [Sources](user.md#sources)).
- `Dockerfile`, `start.sh`, `healthcheck.sh`: the image.

## Developing

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/playwright install chromium   # for the browser's tests (the full browser)
.venv/bin/python -m pytest -q
.venv/bin/black --check ex_app tests && .venv/bin/pylint ex_app/lib tests
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

For the app store, the store's certificate for `harvester` then signs the archive, and the
release can be published there as the other apps are.
