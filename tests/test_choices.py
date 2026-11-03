# SPDX-License-Identifier: AGPL-3.0-or-later
"""A recipe that goes through choices (harvester.choices, the step "each"; harvester.sources
.choose): each option of a <select> (an account), each entry of a custom dropdown (a child), a
fixed list of values (years), the steps after the step and the walk done again for each, the
choice's text in file names and keys; in Chromium against the fake site (routed, no network), the
recipe's checks, the recipe's test listing the choices, and the recorder's Steps list turning a
recorded choice into one repeated for each option."""

# pylint: disable=missing-function-docstring,protected-access

from test_browser import FREE, needs_chromium
from test_clicks import run, source

from fake_site import PDF, SITE, Site
from harvester import choices
from harvester.browser.steps import StepList
from harvester.recipe import check_recipe, step_label, uses_browser
from harvester.sources import site as site_module

# The bank: an account chosen in a list opens its statements, whose files have the same addresses
# for every account (the account is the session's); an option disabled, a placeholder.
ACCOUNTS = """<html><body><select id="account" onchange="location.href = '/statements?account=' + this.value">
<option value="">Choose an account</option><option value="1">Checking</option><option value="2">Savings</option>
<option value="3" disabled>Closed</option></select></body></html>"""
# The school portal: a dropdown of children, each shown in place with its files.
PORTAL = """<html><body><button class="child" onclick="menu.hidden = !menu.hidden">Child</button>
<ul id="menu" class="menu" hidden><li>Anna</li><li>Ben</li></ul><div id="files"></div><script>
for (const li of menu.querySelectorAll('li')) li.onclick = () => {
  menu.hidden = true
  files.innerHTML = `<a class="file" href="/reports/${li.textContent.toLowerCase()}.pdf">Report</a>`
}
</script></body></html>"""


class Bank(Site):  # pylint: disable=too-few-public-methods
    """The fake site with the bank's and the school's pages, and the years' listings."""

    def answer(self, url: str, method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        path = url.split("://", 1)[1].split("/", 1)[1]
        html = {"content-type": "text/html"}
        if path == "accounts":
            self.requests.append(url)
            return 200, html, ACCOUNTS.encode()
        if path.startswith("statements?account="):
            self.requests.append(url)
            links = '<a class="file" href="/doc/1.pdf">Statement 1</a><a class="file" href="/doc/2.pdf">Statement 2</a>'
            return 200, html, f"<html><body>{links}</body></html>".encode()
        if path == "portal":
            return 200, html, PORTAL.encode()
        if path.startswith("years/"):
            year = path.split("/")[1]
            return 200, html, f'<html><body><a class="file" href="/y/{year}.pdf">{year}</a></body></html>'.encode()
        if path.startswith(("doc/", "reports/", "y/")):
            self.requests.append(url)
            return 200, {"content-type": "application/pdf"}, PDF
        return super().answer(url, method, cookie)


EACH_ACCOUNT = {
    "steps": [{"action": "goto", "url": f"{SITE}/accounts"}, {"action": "each", "selector": "select#account"}],
    "links": {"selector": "a.file", "pattern": r"\.pdf$"},
}


@needs_chromium
def test_each_option_of_a_list_is_chosen_and_its_listing_walked(monkeypatch):
    site = Bank()
    message, library, log = run(source(site, monkeypatch, EACH_ACCOUNT, path="{choice}/{filename}"))
    assert message == "4 downloaded"
    assert sorted(library.files) == [
        "/In/Checking/1.pdf",
        "/In/Checking/2.pdf",
        "/In/Savings/1.pdf",
        "/In/Savings/2.pdf",
    ]
    statements = [url for url in site.requests if "/statements" in url]
    assert statements == [f"{SITE}/statements?account=1", f"{SITE}/statements?account=2"]
    assert "going through 2 choices: Checking and Savings" in log
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_a_dropdowns_entries_are_chosen_and_a_choice_that_fails_is_a_gap(monkeypatch):
    recipe = {
        "steps": [
            {"action": "goto", "url": f"{SITE}/portal"},
            {"action": "each", "selector": "button.child", "option": "ul.menu li"},
        ],
        "links": {"selector": "a.file", "pattern": r"/reports/(?P<name>\w+)\.pdf$"},
    }
    message, library, _ = run(source(Bank(), monkeypatch, recipe, path="{choice}-{name}.pdf"))
    assert message == "2 downloaded" and sorted(library.files) == ["/In/Anna-anna.pdf", "/In/Ben-ben.pdf"]
    # A pattern on the texts keeps some; a fixed value the dropdown lacks fails alone.
    narrowed = recipe | {"steps": [recipe["steps"][0], recipe["steps"][1] | {"values": ["Ben", "Carl"]}]}
    made = source(Bank(), monkeypatch, narrowed, path="{choice}.pdf")
    _, library, log = run(made)
    assert sorted(library.files) == ["/In/Ben.pdf"] and made.gaps == 1
    assert any(line.startswith("the choice “Carl” failed:") for line in log)


@needs_chromium
def test_a_fixed_list_of_values_fills_the_listings_address(monkeypatch):
    recipe = {
        "steps": [{"action": "each", "values": ["2025", "2026"]}],
        "start": f"{SITE}/years/{{choice}}",
        "links": {"selector": "a.file", "pattern": r"\.pdf$", "key": "{choice}-{filename}"},
    }
    made = source(Bank(), monkeypatch, recipe, path="{choice}.pdf")
    try:
        items = list(made.items(lambda key: False))
    finally:
        made.close()
    assert [(item.path, item.url) for item in items] == [
        ("2025.pdf", f"{SITE}/y/2025.pdf"),
        ("2026.pdf", f"{SITE}/y/2026.pdf"),
    ]
    assert items[0].key == "choice:2025#key:2025-2025.pdf"
    assert items[1].values["choice_value"] == "2026"


@needs_chromium
def test_the_recipes_test_lists_the_choices(monkeypatch):
    made = source(Bank(), monkeypatch, EACH_ACCOUNT | {"steps": [*EACH_ACCOUNT["steps"]]})
    made.settings["path"] = "{choice}/{filename}"
    try:
        result = made.survey()
    finally:
        made.close()
    assert result["choices"] == ["Checking", "Savings"] and result["ended"] == "complete"
    assert [page["choice"] for page in result["pages"]] == ["Checking", "Savings"]
    assert result["files"] == 4


def test_the_step_each_is_checked():
    def problems(step: dict, before: list | None = None) -> list[str]:
        return check_recipe(
            {"steps": [*(before or [{"action": "goto", "url": f"{SITE}/accounts"}]), step], "links": {"selector": "a"}}
        )

    assert not problems({"action": "each", "selector": "select"}) and not problems({"action": "each", "values": ["a"]})
    assert problems({"action": "each"}) == ['step 2: the options of what? (a "selector", or a list of values)']
    assert problems({"action": "each", "values": []}) == [
        'step 2: the values are "options" or a list of 1 to 100 texts'
    ]
    assert problems({"action": "each", "option": "li", "values": ["a"]}) == [
        "step 2: a dropdown's entries (option) need the element that shows them (selector)"
    ]
    assert problems({"action": "each", "selector": "s", "pattern": "("})[0].startswith(
        "step 2: the pattern is not a regular expression"
    )
    assert problems({"action": "each", "selector": "s", "optional": True}) == ["step 2: a choice is never optional"]
    twice = [{"action": "goto", "url": f"{SITE}/a"}, {"action": "each", "values": ["a"]}]
    assert problems({"action": "each", "values": ["b"]}, twice) == [
        "step 3: a recipe goes through one list of choices (one step each)"
    ]
    signing = [
        {"action": "goto", "url": f"{SITE}/login"},
        {"action": "each", "values": ["a"]},
        {"action": "fill", "selector": "#u", "credential": "username"},
        {"action": "fill", "selector": "#p", "credential": "password"},
    ]
    assert problems({"action": "click", "selector": "button"}, signing) == [
        "step 2: the choices come after the sign-in"
    ]
    keyed = {
        "steps": [{"action": "each", "values": ["a"]}],
        "start": f"{SITE}/x",
        "links": {"selector": "a", "key": "{choice}-{url}"},
    }
    assert not check_recipe(keyed) and uses_browser(keyed)
    assert check_recipe(keyed | {"steps": []}) == [
        "file links: the key {choice}-{url} names choice, which no pattern gives"
    ]
    assert (
        step_label({"action": "each", "selector": "select#a"}) == "go through the choices for each option of select#a"
    )
    assert choices.filled({"action": "fill", "value": "{choice}/{choice_value}"}, "Anna", "7")["value"] == "Anna/7"


def test_the_steps_list_repeats_a_recorded_choice_for_each_option():
    steps = StepList()
    steps.load(
        [
            {"action": "goto", "url": f"{SITE}/accounts"},
            {"action": "select", "selector": "#account", "value": "1", "alternatives": ["select"]},
        ]
    )
    assert steps.edit("each", {"step": 1})
    assert steps.all()[1] == {"action": "each", "selector": "#account", "alternatives": ["select"], "values": "options"}
    assert steps.edit("each", {"step": 1, "values": [" 1 ", "", "2"], "pattern": "^Check"})
    assert steps.all()[1]["values"] == ["1", "2"] and steps.all()[1]["pattern"] == "^Check"
    assert steps.edit("each", {"step": 1, "values": "options", "pattern": ""})
    assert steps.all()[1] == {"action": "each", "selector": "#account", "alternatives": ["select"], "values": "options"}
    assert steps.edit("each", {"step": 0}) and steps.all()[0]["action"] == "goto", "another step stays as it is"
    assert not check_recipe({"steps": steps.all(), "links": {"selector": "a"}})
