# SPDX-License-Identifier: AGPL-3.0-or-later
"""A recipe's steps as Chrome DevTools Recorder's recordings, and back: the JSON its Recorder
panel exports and imports, which @puppeteer/replay replays and converts (to Puppeteer,
Playwright, Cypress, WebPageTest). The format's schema is @puppeteer/replay's src/Schema.ts:

    {"title": "...", "steps": [{"type": "setViewport", "width": 1280, ...},
      {"type": "navigate", "url": "https://..."},
      {"type": "click", "selectors": [["aria/Sign in[role=\\"button\\"]"], ["#login"]],
       "offsetX": 5, "offsetY": 5, "frame": [0], "target": "main"},
      {"type": "change", "selectors": [...], "value": "anna"},
      {"type": "keyDown", "key": "Enter"}, {"type": "keyUp", "key": "Enter"},
      {"type": "waitForElement", "selectors": [...], "visible": true}, ...]}

Import (import_flow) makes recipe steps of a recording:

- navigate: goto; setViewport: the recipe's viewport; click and doubleClick (its button):
  click (count 2, button right or middle); hover; change: fill (a list's change too: the replay
  chooses its option); keyDown and keyUp of one key, with the modifiers held: press (in the field
  the step before acted on, else on the page); waitForElement: wait (visible, or hidden for
  count 0 or visible false); a navigation it asserts: the address its step waits for.
- Selectors: each of Chrome's is a list of selectors through shadow roots (Playwright's CSS goes
  into open ones by itself): CSS as it is, pierce/ as CSS, xpath/ as xpath=, text/ as Playwright's
  text=, aria/<name>[role="<role>"] as internal:role (get_by_role); one without a role as its
  aria-label or its text. The first is the step's selector, the others its alternatives.
- frame (indexes of the frames down from the page): a frame path by each frame's place
  (iframe and frame elements in order); target another than main: the steps there go on in the
  popup the step before opened (popup), and back to main in its opener (opener).
- A password typed into a password's field is never kept: that step fills in the job's password,
  and a value typed into another field just before it the job's user name (as the recorder does).
- Harvester's own steps exported as custom steps ("harvester:<action>") come back as they were,
  as does every step exported with its "harvester" copy (export_flow): a round trip loses
  nothing.
- What has no step in a recipe is left out, each with a warning: a scroll to a place (the replay
  scrolls an element into view itself), a script's expression (recipes run no scripts of their
  own: a shared recipe could take the session with it), a close, network conditions, a wait's
  attributes or properties.

Export (export_flow) makes a recording of a recipe's steps that Chrome's Recorder replays as far
as it can: its own kinds of step for what it has, each with Harvester's step beside it
("harvester": the step as it is), and a custom step ("harvester:<action>") for what it has not
(the account's credentials, a code from e-mail or the phone, choices, a popup, a person's step,
scrolling to load more), which its replay passes over. A credential is never in a file: only
which one the job fills in. A step inside a frame says so (Chrome finds frames by their number,
which a recipe does not know): it is replayed on the page there.
"""

import copy
import json
import re
from typing import Any

from . import recipe as recipes
from . import stepoptions

# A recording is a few hundred kilobytes at most; its steps at most so many.
MAX_BYTES = 512_000
MAX_STEPS = 500
# What marks Harvester's own steps among the custom steps, and its copy of a step.
CUSTOM = "harvester:"
OWN = "harvester"
# Where a click lands in its element (Chrome's offsetX and offsetY, required): near its top left,
# inside any button.
OFFSET = 5
# Chrome's names of the modifier keys as the keyboard's left and right ones; Playwright's.
MODIFIER_KEYS = {
    "Shift": "Shift",
    "ShiftLeft": "Shift",
    "ShiftRight": "Shift",
    "Control": "Control",
    "ControlLeft": "Control",
    "ControlRight": "Control",
    "Alt": "Alt",
    "AltLeft": "Alt",
    "AltRight": "Alt",
    "Meta": "Meta",
    "MetaLeft": "Meta",
    "MetaRight": "Meta",
}
# Keys Chrome names otherwise than Playwright.
KEY_NAMES = {" ": "Space", "\r": "Enter", "\n": "Enter", "NumpadEnter": "Enter"}
ARIA = re.compile(r'^(?P<name>.*?)(?:\[role="(?P<role>[\w-]+)"\])?$', re.DOTALL)
ROLE = re.compile(r'^internal:role=(\w+)\[name="((?:[^"\\]|\\.)*)"s?\]$')
TEXT_IS = re.compile(r'^[a-zA-Z][\w-]*:text-is\("((?:[^"\\]|\\.)*)"\)$')


class RecordingError(ValueError):
    """A text that is no recording of Chrome's Recorder."""


def _quoted(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _unquoted(text: str) -> str:
    try:
        return str(json.loads(f'"{text}"'))
    except ValueError:
        return text


def from_chrome_selector(selector: Any) -> list[str]:  # pylint: disable=too-many-return-statements
    """Playwright's selectors of one of Chrome's (a text, or a list through shadow roots): one,
    or two for an ARIA name without a role (its aria-label, else its text); none for one that is
    neither."""
    parts = [selector] if isinstance(selector, str) else selector if isinstance(selector, list) else []
    if not parts or not all(isinstance(part, str) and part.strip() for part in parts):
        return []
    if len(parts) > 1:
        # Through shadow roots: Playwright's CSS goes into open ones, so the last part alone,
        # within the parts before it.
        converted = [from_chrome_selector(part) for part in parts]
        if not all(converted):
            return []
        return [" >> ".join(options[0] for options in converted)]
    part = parts[0].strip()
    if part.startswith("pierce/"):
        return [part[len("pierce/") :]]
    if part.startswith("xpath/"):
        return [f"xpath={part[len('xpath/'):]}"]
    if part.startswith("text/"):
        return [f"text={_quoted(part[len('text/'):])}"]
    if part.startswith("aria/"):
        found = ARIA.match(part[len("aria/") :])
        name, role = (found.group("name"), found.group("role")) if found else (part[5:], None)
        if role:
            return [f"internal:role={role}[name={_quoted(name)}s]"]
        return [f"[aria-label={_quoted(name)}]", f"text={_quoted(name)}"]
    return [part]


def to_chrome_selector(selector: str) -> str | None:
    """Chrome's selector of one of Playwright's that has one: CSS, a role and name (aria/), a
    text (text/), an XPath (xpath/); None for a Playwright engine Chrome lacks (a scope, nth)."""
    selector = selector.strip()
    if found := ROLE.match(selector):
        return f'aria/{_unquoted(found.group(2))}[role="{found.group(1)}"]'
    if found := TEXT_IS.match(selector):
        return f"text/{_unquoted(found.group(1))}"
    if selector.startswith("xpath="):
        return f"xpath/{selector[len('xpath='):]}"
    if selector.startswith("text="):
        text = selector[len("text=") :]
        return f"text/{_unquoted(text[1:-1]) if text.startswith(chr(34)) and text.endswith(chr(34)) else text}"
    if ">>" in selector or selector.startswith("internal:") or ":text-is(" in selector or ":has-text(" in selector:
        return None
    return selector


def _selectors(step: dict) -> list[str]:
    """A Chrome step's selectors as Playwright's, each once, in their order."""
    found: list[str] = []
    for selector in step.get("selectors") or []:
        found += from_chrome_selector(selector)
    return list(dict.fromkeys(found))


def _text_of(step: dict) -> str:
    """The text a click's element shows, from its ARIA name or text selector (the Steps list's
    label of a click)."""
    for selector in step.get("selectors") or []:
        part = selector[-1] if isinstance(selector, list) and selector else selector
        if isinstance(part, str) and part.startswith(("aria/", "text/")):
            found = ARIA.match(part[5:])
            return (found.group("name") if found else part[5:]).strip()[:80]
    return ""


def _frame(step: dict) -> list[dict]:
    """A frame path of Chrome's frame indexes: each frame by its place among the frame elements
    of its parent's document."""
    frame = step.get("frame")
    if not isinstance(frame, list) or not all(isinstance(index, int) and index >= 0 for index in frame):
        return []
    return [{"selector": f":is(iframe, frame) >> nth={index}"} for index in frame]


def _on_element(action: str, step: dict, **fields: Any) -> dict | None:
    """A step on an element of a Chrome step's selectors (None without one), in its frame."""
    found = _selectors(step)
    if not found:
        return None
    made = {"action": action, "selector": found[0], **fields}
    if found[1:]:
        made["alternatives"] = found[1:]
    if frame := _frame(step):
        made["frame"] = frame
    return made


def _navigated(step: dict) -> dict:
    """The wait for the address a Chrome step asserts it navigated to, if it does."""
    for event in step.get("assertedEvents") or []:
        if isinstance(event, dict) and event.get("type") == "navigation" and isinstance(event.get("url"), str):
            url = event["url"]
            if url.startswith(("http://", "https://")):
                return {"wait": {"url": recipes.url_pattern(url)}}
    return {}


class _Importer:  # pylint: disable=too-few-public-methods
    """One recording made steps (import_flow)."""

    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.warnings: list[str] = []
        self.viewport: dict | None = None
        self.held: list[str] = []
        self.target = "main"
        # The steps made here that wait for the address they led to: their next step's element
        # too (recipe.link_waits), not that of a step exported with its own wait.
        self.navigating: list[dict] = []

    def skip(self, number: int, kind: str, why: str) -> None:
        """A step of the recording left out, and why."""
        self.warnings.append(f"step {number} ({kind}) is left out: {why}")

    def add(self, number: int, step: dict) -> None:
        """A step of the recording's in its target: a popup's steps after a step popup."""
        target = str(step.get("target") or "main")
        if target != self.target:
            self.steps.append({"action": "popup" if target != "main" else "opener"})
            self.target = target
        made = self._made(number, step)
        if made is not None:
            self.steps.append(made)
            if _navigated(step) and not isinstance(step.get(OWN), dict):
                self.navigating.append(made)

    def _made(self, number: int, step: dict) -> dict | None:  # pylint: disable=too-many-return-statements
        kind = str(step.get("type") or "")
        own = step.get(OWN)
        if isinstance(own, dict) and isinstance(own.get("action"), str):
            return copy.deepcopy(own)
        if kind == "customStep":
            name = str(step.get("name") or "")
            parameters = step.get("parameters") if isinstance(step.get("parameters"), dict) else {}
            if name.startswith(CUSTOM):
                return {"action": name[len(CUSTOM) :], **copy.deepcopy(parameters)}
            self.skip(number, f"custom step {name}", "a step of another tool's")
            return None
        if kind == "navigate":
            return {"action": "goto", "url": str(step.get("url") or "")}
        if kind == "setViewport":
            width, height = step.get("width"), step.get("height")
            if isinstance(width, int) and isinstance(height, int):
                self.viewport = {"width": width, "height": height}
            return None
        if kind in ("click", "doubleClick"):
            return self._click(number, step, kind)
        if kind == "hover":
            return self._element(number, step, "hover")
        if kind == "change":
            return self._element(number, step, "fill", value=str(step.get("value") or ""))
        if kind in ("keyDown", "keyUp"):
            return self._key(step, kind)
        if kind == "waitForElement":
            return self._wait(number, step)
        reasons = {
            "scroll": "a scroll to a place: the job scrolls an element into view itself",
            "waitForExpression": "a script's expression: recipes run no scripts of their own",
            "close": "the job closes its tabs itself",
            "emulateNetworkConditions": "the job's browser has the network it has",
        }
        self.skip(number, kind or "no type", reasons.get(kind, "no step of a recipe's"))
        return None

    def _element(self, number: int, step: dict, action: str, **fields: Any) -> dict | None:
        made = _on_element(action, step, **fields)
        if made is None:
            self.skip(number, str(step.get("type")), "no selector the job's browser reads")
        return made

    def _click(self, number: int, step: dict, kind: str) -> dict | None:
        button = {"secondary": "right", "auxiliary": "middle"}.get(str(step.get("button") or ""), "")
        fields: dict[str, Any] = {"text": _text_of(step)}
        if button:
            fields["button"] = button
        if kind == "doubleClick":
            fields["count"] = 2
        made = self._element(number, step, "click", **fields)
        return made | _navigated(step) if made is not None else None

    def _key(self, step: dict, kind: str) -> dict | None:
        """A key: modifiers are held until their keyUp; another key's keyDown is a press (with the
        modifiers held), in the field the step before acted on (else on the page); keyUps of other
        keys are part of it."""
        key = str(step.get("key") or "")
        key = KEY_NAMES.get(key, key)
        if key in MODIFIER_KEYS:
            modifier = MODIFIER_KEYS[key]
            if kind == "keyDown" and modifier not in self.held:
                self.held.append(modifier)
            elif kind == "keyUp" and modifier in self.held:
                self.held.remove(modifier)
            return None
        last = self.steps[-1] if self.steps else {}
        if kind == "keyUp" or not key:
            # The navigation a key led to is told at its keyUp: its press waits for it.
            if (
                kind == "keyUp"
                and last.get("action") == "press"
                and last.get("key", "").endswith(key)
                and _navigated(step)
            ):
                last |= _navigated(step)
                self.navigating.append(last)
            return None
        pressed = "+".join([*self.held, key])
        made: dict[str, Any] = {"action": "press", "key": pressed}
        if last.get("action") in ("fill", "click") and last.get("selector"):
            made |= {name: last[name] for name in ("selector", "alternatives", "frame") if name in last}
        return made | _navigated(step)

    def _wait(self, number: int, step: dict) -> dict | None:
        count, operator = step.get("count"), step.get("operator", ">=")
        hidden = step.get("visible") is False or (count == 0 and operator in ("==", "<="))
        if step.get("attributes") or step.get("properties"):
            self.warnings.append(f"step {number} (waitForElement): its attributes and properties are not checked")
        made = self._element(number, step, "wait", state="hidden" if hidden else "visible")
        if made is not None and isinstance(step.get("timeout"), (int, float)) and step["timeout"] > 0:
            made["timeout"] = max(1, min(stepoptions.WAIT_MAX, round(step["timeout"] / 1000)))
        return made


def _no_passwords(steps: list[dict], warnings: list[str]) -> None:
    """A value typed into a password's field is the job's password, and one typed into another
    field just before it its user name: neither stays in the recipe."""
    for index, step in enumerate(steps):
        if step.get("action") != "fill" or "credential" in step:
            continue
        if not recipes.PASSWORD_FIELD.search(str(step.get("selector") or "")) or not step.get("value"):
            continue
        step.pop("value")
        step["credential"] = "password"
        warnings.append(f"the password typed in the recording is not kept: step {index + 1} fills in the job's")
        before = next((other for other in reversed(steps[:index]) if other.get("action") == "fill"), None)
        if before is not None and "credential" not in before and before.get("value"):
            before.pop("value")
            before["credential"] = "username"


def parsed(text: str) -> dict:
    """A recording's JSON (RecordingError for none)."""
    if len(text.encode()) > MAX_BYTES:
        raise RecordingError(f"a recording of Chrome's Recorder is at most {MAX_BYTES // 1000} KB")
    try:
        flow = json.loads(text)
    except ValueError as error:
        raise RecordingError(f"not JSON: {error}") from error
    if not isinstance(flow, dict) or not isinstance(flow.get("steps"), list):
        raise RecordingError("not a recording of Chrome's Recorder: it has no list of steps")
    if len(flow["steps"]) > MAX_STEPS:
        raise RecordingError(f"more than {MAX_STEPS} steps")
    return flow


def import_flow(flow: dict) -> tuple[list[dict], dict | None, list[str]]:
    """A recording's steps as a recipe's, the viewport it sets (None: none), and what was left
    out or changed, each a warning."""
    importer = _Importer()
    for number, step in enumerate(flow.get("steps") or [], 1):
        if isinstance(step, dict):
            importer.add(number, step)
        else:
            importer.warnings.append(f"step {number} is left out: not a step")
    steps = importer.steps
    _no_passwords(steps, importer.warnings)
    for index, step in enumerate(steps):
        if any(step is made for made in importer.navigating):
            # The next step only lends its element: a copy, which link_waits may change.
            recipes.link_waits([step, *copy.deepcopy(steps[index + 1 : index + 2])])
    return steps, importer.viewport, importer.warnings


def _chrome_selectors(step: dict) -> list[list[str]]:
    found = [to_chrome_selector(value) for value in [step.get("selector"), *(step.get("alternatives") or [])] if value]
    return [[value] for value in dict.fromkeys(value for value in found if value)]


def _keys(key: str) -> list[dict]:
    """A press's key as Chrome's keyDown and keyUp, its modifiers held around it."""
    if key == "+" or key.endswith("++"):
        modifiers, last = key[:-2].split("+") if len(key) > 1 else [], "+"
    else:
        *modifiers, last = key.split("+")
    modifiers = [modifier for modifier in modifiers if modifier]
    last = {"Space": " "}.get(last, last)
    downs = [{"type": "keyDown", "key": modifier} for modifier in [*modifiers, last]]
    ups = [{"type": "keyUp", "key": modifier} for modifier in [last, *reversed(modifiers)]]
    return downs + ups


def _custom(step: dict) -> dict:
    return {
        "type": "customStep",
        "name": CUSTOM + str(step["action"]),
        "parameters": {k: v for k, v in step.items() if k != "action"},
    }


def _exported(step: dict, warnings: list[str], number: int) -> list[dict]:  # pylint: disable=too-many-return-statements
    """A recipe step as Chrome's (one, or a key's keyDown and keyUp), each with the step itself
    beside it; a custom step for one Chrome has none for."""
    action = step.get("action")
    own = {OWN: copy.deepcopy(step)}
    selectors = _chrome_selectors(step) if step.get("selector") else []
    if step.get("frame"):
        warnings.append(
            f"step {number} acts in a frame, which Chrome's Recorder finds by its number: there it acts on the page"
        )
    if action == "goto":
        return [{"type": "navigate", "url": step.get("url", ""), **own}]
    if action in ("click", "hover", "wait", "fill", "select") and step.get("selector") and not selectors:
        warnings.append(f"step {number}: its selectors are Playwright's own, which Chrome's Recorder does not read")
        return [_custom(step)]
    if action == "click":
        kind = "doubleClick" if step.get("count") == 2 else "click"
        button = {"right": "secondary", "middle": "auxiliary"}.get(str(step.get("button") or ""), "primary")
        return [{"type": kind, "selectors": selectors, "offsetX": OFFSET, "offsetY": OFFSET, "button": button, **own}]
    if action == "hover":
        return [{"type": "hover", "selectors": selectors, **own}]
    if action == "fill" and "credential" not in step:
        return [{"type": "change", "selectors": selectors, "value": str(step.get("value", "")), **own}]
    if action == "select" and not isinstance(step.get("value"), list):
        return [
            {
                "type": "change",
                "selectors": selectors,
                "value": str(step.get("value") or step.get("label") or ""),
                **own,
            }
        ]
    if action == "wait":
        visible = step.get("state") != "hidden"
        made = {
            "type": "waitForElement",
            "selectors": selectors,
            "timeout": int(stepoptions.wait_timeout(step) * 1000),
            **own,
        }
        return [made | ({"visible": True} if visible else {"count": 0, "operator": "=="})]
    if action == "press" and recipes.key_ok(step.get("key", "Enter")):
        keys = _keys(str(step.get("key", "Enter")))
        keys[0][OWN] = own[OWN]
        # The rest belong to the first: an import of them makes no steps of their own.
        for key in keys[1:]:
            key[OWN + "Part"] = True
        return keys
    return [_custom(step)]


def export_flow(recipe: dict, title: str = "") -> tuple[dict, list[str]]:
    """The recipe's steps (and viewport) as a recording of Chrome's Recorder, and what Chrome's
    replay cannot do of them, each a warning."""
    warnings: list[str] = []
    steps: list[dict] = []
    viewport = recipe.get("viewport") if isinstance(recipe.get("viewport"), dict) else {"width": 1280, "height": 800}
    steps.append(
        {
            "type": "setViewport",
            "width": int(viewport.get("width") or 1280),
            "height": int(viewport.get("height") or 800),
            "deviceScaleFactor": 1,
            "isMobile": False,
            "hasTouch": False,
            "isLandscape": False,
        }
    )
    for number, step in enumerate(recipe.get("steps") or [], 1):
        if isinstance(step, dict):
            steps += _exported(step, warnings, number)
    if any(step.get("type") == "customStep" for step in steps):
        warnings.append(
            "Harvester's own steps (a credential, a code, a person's step) are custom steps,"
            " which Chrome's replay passes over"
        )
    name = title.strip() or str(recipe.get("start") or "") or "Harvester recipe"
    return {"title": name[:200], "steps": steps}, warnings


def import_text(text: str) -> tuple[list[dict], dict | None, list[str]]:
    """import_flow of a recording's text, the parts of a key exported with its press dropped."""
    flow = parsed(text)
    flow = flow | {"steps": [step for step in flow["steps"] if not (isinstance(step, dict) and step.get(OWN + "Part"))]}
    return import_flow(flow)
