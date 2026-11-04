# SPDX-License-Identifier: AGPL-3.0-or-later
"""The page's Save to and Tag as (the form's section: the fields of each place, the vault's
warning and password, what a place switches off with why, no Nextcloud tags outside the files),
a job's Files with their labels, and the Library (set up with its recovery key typed back,
locked until unlocked, its token sent as a header and never in an address but a ticket's, the
cookie's addresses where the cookie comes back, thumbnails, the list, the viewer, uploads in
pieces, a move from Files and its states readable in light and dark), the dialogs centred at
any width, in Chromium against a stubbed API."""

# pylint: disable=missing-function-docstring,unused-import,redefined-outer-name

import json

from test_browser import needs_chromium
from test_page import CALLS, PAGE, fixture_page  # noqa: F401  # the page's fixture

ENTRIES = [
    {"id": 2, "path": "/Mag/2024", "name": "2024", "dir": True, "size": 0, "type": "", "labels": [], "job": 1, "at": 1},
    {
        "id": 3,
        "path": "/Mag/cover.png",
        "name": "cover.png",
        "dir": False,
        "size": 10,
        "type": "image/png",
        "labels": ["2024"],
        "job": 1,
        "at": 1,
    },
    {
        "id": 4,
        "path": "/Mag/issue.pdf",
        "name": "issue.pdf",
        "dir": False,
        "size": 2000,
        "type": "application/pdf",
        "labels": ["2024"],
        "job": 1,
        "at": 1,
    },
]


# The library's token each request came with, by path.
TOKENS: list[tuple[str, str]] = []


def answering(page, answers: dict) -> None:
    """The library's and the job's routes, before the fixture's (each answer by method and path)."""

    def route(handled) -> None:
        method = handled.request.method
        full = handled.request.url.split("/api", 1)[1]
        path = full.split("?")[0]
        # An upload's piece is the file's bytes, anything else JSON.
        sent = handled.request.post_data_buffer
        is_json = handled.request.headers.get("content-type", "") == "application/json"
        CALLS.append((method, full, (json.loads(sent) if is_json else sent) if sent else None))
        TOKENS.append((path, handled.request.headers.get("x-harvester-library", "")))
        answer = answers.get(f"{method} {path}")
        if answer is None:
            handled.fallback()
            return
        if isinstance(answer, tuple) and len(answer) == 3:
            handled.fulfill(status=answer[0], content_type=answer[1], body=answer[2])
            return
        status, data = answer if isinstance(answer, tuple) else (200, answer)
        handled.fulfill(status=status, content_type="application/json", body=json.dumps(data))

    page.route("**/apps/app_api/proxy/harvester/api/**", route)


@needs_chromium
def test_save_to_shows_each_places_fields_and_switches_off_what_it_cannot_do(page):
    answering(page, {"PUT /jobs/1": {"id": 1, "name": "Mag"}, "PUT /jobs/1/vault": {}})
    page.get_by_role("button", name="Edit").click()
    place = page.locator("select[data-option=save_to]")
    tag_as = page.locator("select[data-option=tag_as]")
    assert place.input_value() == "files" and tag_as.input_value() == "nextcloud", "a job of before as it was"
    assert not page.locator("input[data-option=vault]").is_visible()
    assert not page.locator(".harvester-vault-warning").is_visible()
    place.select_option("inbox")
    assert (
        page.locator("input[data-option=vault]").is_visible()
        and page.locator("input[data-option=vault_target]").is_visible()
    )
    assert tag_as.input_value() == "labels" and page.locator(
        "select[data-option=tag_as] option[value=nextcloud]"
    ).evaluate("o => o.disabled")
    again = page.locator("input[data-option=again_deleted]")
    assert again.is_disabled() and "Off for an inbox" in page.locator(".harvester-off-reason").first.inner_text()
    place.select_option("library")
    assert not again.is_disabled() and page.locator(".harvester-off-reason").count() == 0
    place.select_option("vault")
    warning = page.locator(".harvester-vault-warning")
    assert warning.is_visible() and warning.get_attribute("role") == "alert"
    assert "whoever takes over the server can read the whole vault" in warning.inner_text()
    page.locator("input[data-option=vault]").fill("/Vaults/Harvester")
    page.locator("input[data-field=vault-password]").fill("vault secret")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_function("() => !document.querySelector('#harvester-job-form')")
    saved = next(body for method, path, body in CALLS if (method, path) == ("PUT", "/jobs/1"))
    assert saved["options"]["save_to"] == "vault" and saved["options"]["vault"] == "/Vaults/Harvester"
    assert saved["options"]["tag_as"] == "labels"
    assert ("PUT", "/jobs/1/vault", {"password": "vault secret"}) in CALLS
    assert all("vault secret" not in json.dumps(body) for method, path, body in CALLS if path != "/jobs/1/vault")


@needs_chromium
def test_save_to_immich_shows_the_album_and_the_persons_account(page):
    answering(
        page,
        {
            "PUT /jobs/1": {"id": 1, "name": "Mag"},
            "GET /immich": {"url": "", "preset": "https://photos.example.org", "key": False, "http": False},
            "PUT /immich": {"email": "anna@example.org", "name": "Anna Example"},
        },
    )
    page.get_by_role("button", name="Edit").click()
    page.locator("select[data-option=save_to]").select_option("immich")
    tag_as = page.locator("select[data-option=tag_as]")
    assert tag_as.input_value() == "immich", "Immich's own tags, as Nextcloud's in the files"
    assert page.locator("select[data-option=tag_as] option[value=nextcloud]").evaluate("o => o.disabled")
    assert page.locator("input[data-option=recheck]").is_disabled()
    account = page.locator(".harvester-immich-account")
    account.locator("summary").click()
    page.wait_for_function("() => document.querySelector('[data-field=immich-url]').placeholder.includes('photos')")
    assert "No API key yet" in account.inner_text()
    page.locator("input[data-field=immich-key]").fill("immich secret")
    account.get_by_role("button", name="Save and test").click()
    page.wait_for_function("() => document.querySelector('.harvester-immich-account').innerText.includes('anna@')")
    assert page.locator("input[data-field=immich-key]").input_value() == ""
    assert ("PUT", "/immich", {"url": "", "key": "immich secret"}) in CALLS
    page.locator("input[data-option=immich_album]").fill("Magazines {year}")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_function("() => !document.querySelector('#harvester-job-form')")
    saved = next(body for method, path, body in CALLS if (method, path) == ("PUT", "/jobs/1"))
    assert saved["options"]["save_to"] == "immich" and saved["options"]["immich_album"] == "Magazines {year}"
    assert saved["options"]["tag_as"] == "immich"
    assert all("immich secret" not in json.dumps(body) for method, path, body in CALLS if path != "/immich")


@needs_chromium
def test_a_jobs_files_with_their_labels(page):
    answering(
        page,
        {
            "GET /jobs/1/files": {
                "files": [{"file": 3, "path": "/Mag/cover.png", "at": 1, "tags": ["2024"]}],
                "labels": ["2024"],
            }
        },
    )
    page.get_by_role("button", name="Files", exact=True).click()
    page.wait_for_selector(".harvester-file-list")
    assert "/Mag/cover.png" in page.locator(".harvester-file-list").inner_text()
    page.get_by_label("Tag or label").select_option("2024")
    page.wait_for_function(
        "() => document.querySelector('select') && document.querySelector('select').value === '2024'"
    )
    assert ("GET", "/jobs/1/files?label=2024", None) in CALLS


LIBRARY = {"setup": True, "unlocked": False, "cookie": False, "minutes": 15, "usage": 2010, "limit": 10_000_000_000}


@needs_chromium
def test_the_library_unlocks_and_shows_its_files_without_a_token_in_an_address(page):
    state = dict(LIBRARY)
    answers = {
        "GET /library/state": state,
        "GET /library/moves": [],
        "POST /library/unlock": (403, {"detail": "Wrong password."}),
        "GET /library": {"folder": "/Mag", "entries": ENTRIES, "labels": ["2024"]},
        "GET /library/files/3/thumbnail": (200, "image/jpeg", b"\xff\xd8not really"),
        "POST /library/files/4/ticket": {"ticket": "t1ck3t"},
    }
    ENTRIES[1]["thumb"] = True
    answering(page, answers)
    page.get_by_role("button", name="Library", exact=True).click()
    password = page.get_by_label("Library password")
    password.fill("wrong")
    page.get_by_role("button", name="Unlock").click()
    page.wait_for_selector("text=Wrong password.")
    answers["POST /library/unlock"] = {"token": "t0k3n"}
    state["unlocked"] = True
    password.fill("right")
    page.get_by_role("button", name="Unlock").click()
    page.wait_for_selector(".harvester-gallery img[src^='blob:']")
    assert ("/library/files/3/thumbnail", "t0k3n") in TOKENS, "the thumbnail fetched with the token"
    listed = page.locator(".harvester-library > .harvester-file-list").inner_text()
    assert "2024/" in listed and "issue.pdf" in listed
    assert ("/library", "t0k3n") in TOKENS, "the listing asked with the browser's token"
    assert "2.0 KB of 10.0 GB used" in page.locator(".harvester-library").inner_text()
    page.get_by_role("button", name="issue.pdf").click()
    viewer = page.locator("dialog.harvester-viewer")
    viewer.locator("iframe").wait_for()
    assert viewer.locator("iframe").get_attribute("src").endswith("/library/files/4?ticket=t1ck3t")
    assert "t0k3n" not in page.content(), "the token is in no address"
    viewer.get_by_role("button", name="Close").click()
    assert not viewer.count()
    # Where AppAPI passes the cookie, the files' own addresses.
    state["cookie"] = True
    page.get_by_role("button", name="2024/").click()
    page.wait_for_selector(".harvester-gallery img[src$='/library/files/3/thumbnail']")
    ENTRIES[1].pop("thumb")


@needs_chromium
def test_the_library_is_set_up_with_its_recovery_key_typed_back(page):
    state = LIBRARY | {"setup": False}
    answers = {
        "GET /library/state": state,
        "GET /library/moves": [],
        "POST /library/setup/start": {"recovery": "ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ23-4567"},
        "POST /library/setup": (403, {"detail": "That is not the recovery key shown."}),
        "GET /library": {"folder": "/", "entries": [], "labels": []},
    }
    answering(page, answers)
    page.get_by_role("button", name="Library", exact=True).click()
    page.get_by_label("Library password").fill("long password")
    page.get_by_label("The password again").fill("other password")
    page.get_by_role("button", name="Next: your recovery key").click()
    page.wait_for_selector("text=The passwords differ.")
    page.get_by_label("The password again").fill("long password")
    page.get_by_role("button", name="Next: your recovery key").click()
    assert page.locator(".harvester-recovery-key").inner_text() == "ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ23-4567"
    page.get_by_label("Type the recovery key again").fill("abcd")
    page.get_by_role("button", name="Set up the library").click()
    assert page.get_by_label("I saved my recovery key.").evaluate("box => !box.checkValidity()"), "asked first"
    page.get_by_label("I saved my recovery key.").check()
    page.get_by_role("button", name="Set up the library").click()
    page.wait_for_selector("text=That is not the recovery key shown.")
    assert ("POST", "/library/setup", {"password": "long password", "recovery": "abcd", "minutes": 15}) in CALLS
    answers["POST /library/setup"] = {"token": "n3w"}
    state |= {"setup": True, "unlocked": True}
    page.get_by_label("Type the recovery key again").fill("ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ23-4567")
    page.get_by_role("button", name="Set up the library").click()
    page.wait_for_selector("text=Nothing here yet")
    assert ("/library", "n3w") in TOKENS


@needs_chromium
def test_files_upload_in_pieces_and_a_move_from_files_is_started_on_the_page(page):
    moves = [
        {
            "id": "m1",
            "selected": [{"fileId": 5, "name": "Tax", "directory": "/Docs", "dir": True}],
            "folder": "/From Files",
            "started": 0,
            "ended": 0,
            "problem": "",
            "trash": "",
            "items": [],
        }
    ]
    answers = {
        "GET /library/state": LIBRARY | {"unlocked": True},
        "GET /library/moves": moves,
        "GET /library": {"folder": "/", "entries": [], "labels": []},
        "POST /library/uploads": {"upload": "u1"},
        "PUT /library/uploads/u1": {"received": 3},
        "POST /library/uploads/u1/done": {"id": 9},
        "POST /library/moves/m1/start": moves[0] | {"started": 1},
    }
    answering(page, answers)
    page.goto(PAGE + "?fileIds=5")
    page.wait_for_selector("text=Move 1 item from Files")
    assert "/Docs/Tax/" in page.locator(".harvester-moves").inner_text()
    assert "fileIds" not in page.url, "the address loses it"
    page.get_by_label("Into the library folder").fill("/Taxes")
    moves[0] |= {
        "started": 1,
        "ended": 2,
        "items": [
            {
                "file_id": 6,
                "path": "/Docs/Tax/a.pdf",
                "size": 3,
                "target": "/Taxes/Tax/a.pdf",
                "state": "moved",
                "message": "",
            }
        ],
    }
    page.locator(".harvester-moves").get_by_role("button", name="Move").click()
    page.wait_for_selector("text=Moved from Files: 1 moved, 0 failed")
    assert ("POST", "/library/moves/m1/start", {"folder": "/Taxes"}) in CALLS
    with page.expect_response("**/library/uploads/u1/done"):
        page.locator("input[type=file]").set_input_files(
            {"name": "note.txt", "mimeType": "text/plain", "buffer": b"abc"}
        )
    assert ("POST", "/library/uploads", {"folder": "/", "name": "note.txt", "size": 3}) in CALLS
    assert ("PUT", "/library/uploads/u1?offset=0", b"abc") in CALLS
    assert ("POST", "/library/uploads/u1/done", None) in CALLS


def centred(page, selector: str) -> None:
    """The dialog lies inside the window, as far from its left as its right, from its top as
    its bottom."""
    box = page.locator(selector).bounding_box()
    width, height = page.viewport_size["width"], page.viewport_size["height"]
    assert box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= width, (box, width)
    assert abs(box["x"] - (width - box["x"] - box["width"])) <= 2, (box, width)
    assert abs(box["y"] - (height - box["y"] - box["height"])) <= 2, (box, height)


# Margins as a site's styles may give every element (Nextcloud resets them), which a modal
# <dialog> needs for centring.
RESET = "* { margin: 0 }"


@needs_chromium
def test_the_viewer_and_the_questions_are_centred_at_any_width(page):
    answers = {
        "GET /library/state": LIBRARY | {"unlocked": True},
        "GET /library/moves": [],
        "GET /library": {"folder": "/Mag", "entries": ENTRIES, "labels": ["2024"]},
        "POST /library/files/4/ticket": {"ticket": "t1ck3t"},
    }
    answering(page, answers)
    page.add_style_tag(content=RESET)
    page.get_by_role("button", name="Library", exact=True).click()
    for width, height in ((1280, 800), (1280, 500), (375, 700)):
        page.set_viewport_size({"width": width, "height": height})
        page.get_by_role("button", name="issue.pdf").click()
        page.locator("dialog.harvester-viewer iframe").wait_for()
        centred(page, "dialog.harvester-viewer")
        expected = min(1100, width - 32)
        assert page.locator("dialog.harvester-viewer").bounding_box()["width"] == expected, "the viewer is wide"
        page.locator("dialog.harvester-viewer").get_by_role("button", name="Delete").click()
        page.locator("dialog.harvester-dialog:not(.harvester-viewer)").wait_for()
        centred(page, "dialog.harvester-dialog")
        page.locator("dialog.harvester-dialog").get_by_role("button", name="Cancel").click()
        assert not page.locator("dialog").count()


# Nextcloud's colours, light and dark (its themes' variables), for the states' contrast.
THEMES = {
    "light": {
        "--color-main-background": "#ffffff",
        "--color-main-text": "#222222",
        "--color-background-dark": "#ededed",
        "--color-primary-element-light": "#e5f0f5",
        "--color-primary-element-light-text": "#00293f",
        "--color-success": "#2d7b41",
        "--color-success-text": "#286c39",
        "--color-error": "#db0606",
        "--color-error-text": "#c90000",
    },
    "dark": {
        "--color-main-background": "#171717",
        "--color-main-text": "#ebebeb",
        "--color-background-dark": "#2a2a2a",
        "--color-primary-element-light": "#132b3a",
        "--color-primary-element-light-text": "#d8eaf5",
        "--color-success": "#2a7c47",
        "--color-success-text": "#6cc185",
        "--color-error": "#e9322d",
        "--color-error-text": "#ff7b7b",
    },
}
# The contrast of an element's text on its background laid over the page's (WCAG 2), in the
# browser: its colours as Chromium computes them, rgb() or color(srgb …), with their alpha.
CONTRAST = """(chip) => {
  const parse = (text) => {
    const numbers = text.match(/[\\d.]+/g).map(Number)
    const srgb = text.startsWith('color(')
    const [r, g, b] = numbers.slice(0, 3).map((value) => (srgb ? value : value / 255))
    return [r, g, b, numbers.length > 3 ? numbers[3] : 1]
  }
  const over = (top, bottom) => top.slice(0, 3).map((value, i) => value * top[3] + bottom[i] * (1 - top[3]))
  const page = parse(getComputedStyle(document.body).backgroundColor)
  const style = getComputedStyle(chip)
  const back = over(parse(style.backgroundColor), page)
  const fore = over(parse(style.color), back)
  const light = (rgb) => rgb.map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
    .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0)
  const [a, b] = [light(back), light(fore)].sort((x, y) => y - x)
  return (a + 0.05) / (b + 0.05)
}"""


@needs_chromium
def test_a_moves_states_are_readable_in_light_and_dark(page):
    states = ("waiting", "moving", "moved", "failed")
    items = [
        {"file_id": n, "path": f"/Docs/{state}.pdf", "size": 3, "target": "/T", "state": state, "message": ""}
        for n, state in enumerate(states)
    ]
    move = {"id": "m1", "selected": [], "folder": "/", "started": 1, "ended": 2, "problem": "", "trash": ""}
    answers = {
        "GET /library/state": LIBRARY | {"unlocked": True},
        "GET /library/moves": [move | {"items": items}],
        "GET /library": {"folder": "/", "entries": [], "labels": []},
    }
    answering(page, answers)
    page.get_by_role("button", name="Library", exact=True).click()
    page.wait_for_selector(".harvester-move-failed")
    for theme, colours in THEMES.items():
        variables = "; ".join(f"{name}: {value}" for name, value in colours.items())
        page.add_style_tag(content=f":root {{ {variables} }} body {{ background: var(--color-main-background) }}")
        for state in states:
            ratio = page.locator(f".harvester-move-{state}").evaluate(CONTRAST)
            assert ratio >= 4.5, f"{state} in {theme}: {ratio:.2f}"


@needs_chromium
def test_a_fresh_move_from_files_opens_the_library_without_file_ids_and_after_a_reload(page):
    move = {
        "id": "m1",
        "selected": [{"fileId": 5, "name": "Tax", "directory": "/Docs", "dir": True}],
        "folder": "/From Files",
        "started": 0,
        "ended": 0,
        "problem": "",
        "trash": "",
        "items": [],
        "age": 2,
    }
    answers = {
        "GET /library/state": LIBRARY | {"unlocked": True},
        "GET /library/moves": [move],
        "GET /library": {"folder": "/", "entries": [], "labels": []},
    }
    answering(page, answers)
    for _ in range(2):  # AppAPI's redirect without ?fileIds=, then a reload
        page.goto(PAGE)
        page.wait_for_selector("text=Move 1 item from Files")
    move["age"] = 3600
    page.goto(PAGE)
    page.wait_for_selector("table.harvester-jobs")
    assert not page.locator(".harvester-moves").count(), "an old proposal leaves the jobs page alone"
    move["age"] = 2
    page.goto(PAGE + "?fetch=https%3A%2F%2Fsite.example%2Fa")
    page.wait_for_selector("text=https://site.example/a")
    assert not page.locator(".harvester-moves").count(), "the bookmarklet's address first"
