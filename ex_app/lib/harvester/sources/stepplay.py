# SPDX-License-Identifier: AGPL-3.0-or-later
"""The replay of the kinds of step harvester.stepoptions describes (sources.site.replay does the
step): the answer to a dialog a step opens, a fill typed key by key or of a list, a step "wait"."""

import contextlib

from playwright.sync_api import Error as PlaywrightError

from .. import stepoptions

# The pause between the keys a fill types key by key (ms): a person's pace, that a field's
# script keeps up with.
TYPING_DELAY = 40


def answering(page, step: dict):
    """The dialog the step may open (alert, confirm, prompt, beforeunload) answered as the step
    says (its "dialog" and "prompt"), as a context manager around the step; a step without one
    leaves dialogs to Playwright, which dismisses them."""

    @contextlib.contextmanager
    def answered():
        def answer(dialog) -> None:
            try:
                if step["dialog"] == "accept":
                    dialog.accept(str(step.get("prompt", "")) if dialog.type == "prompt" else "")
                else:
                    dialog.dismiss()
            except PlaywrightError:
                pass  # the page closed or answered it meanwhile

        page.on("dialog", answer)
        try:
            yield
        finally:
            page.remove_listener("dialog", answer)

    return answered() if step.get("dialog") in stepoptions.DIALOG_ANSWERS else contextlib.nullcontext()


def typed(target, selector: str, text: str, wait: dict) -> None:
    """The text typed into the field key by key (a step's "typed": a field that ignores a value set
    at once), after the field is emptied."""
    target.fill(selector, "", **wait)
    target.locator(selector).first.press_sequentially(text, delay=TYPING_DELAY)


def filled_in(target, selector: str, text: str, wait: dict) -> None:
    """A fill: the field's value set; a list (<select>, which a fill from Chrome's Recorder may
    name: its change of a list) chosen by that value instead."""
    try:
        target.fill(selector, text, **wait)
    except PlaywrightError:
        try:
            tag = target.locator(selector).first.evaluate("(element) => element.tagName", timeout=1000)
        except PlaywrightError:
            tag = ""
        if str(tag).upper() != "SELECT":
            raise
        target.select_option(selector, value=text, **wait)


def waited(target, step: dict, selector: str) -> None:
    """A step "wait": until its element shows (with its text in it, if it has one) or is gone,
    within its time (recipe.wait_timeout); Playwright's TimeoutError when it is up."""
    found = target.locator(selector)
    if step.get("text"):
        found = found.filter(has_text=str(step["text"]))
    state = "hidden" if step.get("state") == "hidden" else "visible"
    found.first.wait_for(state=state, timeout=stepoptions.wait_timeout(step) * 1000)
