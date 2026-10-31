# Harvester

A Nextcloud External App (AppAPI, Python) that fetches files from websites into your folders
on a schedule and tags them. It is generic: a site is described by a **recipe**, recorded in
the app's browser or taken from a template, not by code.

- **Jobs:** a recipe and its settings, a folder, a cron schedule (UTC), tags and an account.
  *Harvester* in Nextcloud's top menu lists your jobs, runs one now and edits them.
- **Recipes** (`ex_app/lib/harvester/recipe.py`): the steps to sign in (replayed in Chromium;
  without steps the pages are fetched over HTTP), the listing to start at, levels of links to
  follow (a year, then its issues), the file links, and the next page's link. Each level has
  a CSS selector and a pattern: a regular expression on the link's URL whose named groups
  (`(?P<year>\d{4})`) become values.
- **Values:** the download URL (`{url}/download`), the file's name (`{year}/iX {year}-{issue:02d}`)
  and the tags (`{year}`) are templates of the link's values: the pattern's groups and `url`,
  `text`, `page`, `host`, `filename`, `stem`, `ext`.
- **Recorder** (`ex_app/lib/harvester/browser/`): a headless Chromium in the app, shown on the
  page and driven from it over a WebSocket. Sign in and go to the listing (recorded as steps),
  browse without recording, then pick the links to follow and the file links: the app proposes
  a pattern with named groups for each (`patterns.propose`), outlines what it matches in the
  page (solid) and what it leaves out (dashed), lists examples with their values, and proposes
  a file name and tags. Edit a pattern and the outlines follow. Passwords are never recorded:
  the job fills in its account.
- **Sessions and challenges:** a job keeps its browser session (cookies, local storage;
  encrypted in Nextcloud, like its account) and replays the sign-in only when the listing shows
  a login form. When a site asks for a person instead (reCAPTCHA, hCaptcha, Cloudflare, "verify
  you are human") the run stops, the job shows **Needs you** and its user gets a notification;
  scheduled runs wait. **Continue in the browser** opens the recorder's browser with the job's
  session at that page: the person answers it, **Done** keeps the session and runs the job.
  Harvester never answers a challenge itself.
- **Downloads:** a pause between files; waits when the site asks (429 or 503 with
  `Retry-After`, or a wait pattern on the URL it redirects to, like heise's `wait_sec=`); never a
  web page instead of a file; an expected file type (`pdf`, `zip`, `image`, ...) and size.
- **Repeat runs:** a job remembers what it downloaded; a file already in the folder is tagged
  and not downloaded again. At most `max_items` files per run, the rest comes next time. Once a
  run went through the whole site, a listing of known files ends the next runs.
- **Templates** (`ex_app/lib/harvester/presets/*.json`): jobs for known sites with a few
  parameters, e.g. `heise.json`: heise's magazines (iX, c't, ...) as PDF for a subscription.
- **Accounts:** a job's username and password are your sensitive preferences in Nextcloud
  (encrypted); the page only shows whether they are set.
- **Safety:** the browser and the downloads reach only http(s) addresses outside local and
  private networks (a `file://` page would show the app's secret).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/playwright install --only-shell chromium   # for the browser's tests
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
