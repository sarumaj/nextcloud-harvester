# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recorder's screen against a page that never stops drawing (routed, no network): every
second frame of Chromium's screencast, none while the person's page is hidden, again once it
shows."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio

from test_browser import needs_chromium
from test_recorder_features import recording

from fake_site import SITE, Site
from harvester.browser import recorder as recorder_module

# A page that changes on every frame of the browser's: a screencast sends what it can.
DRAWING = """<html><body><div id="box" style="width: 400px; height: 300px"></div><script>
let hue = 0
const draw = () => { hue = (hue + 7) % 360; box.style.background = `hsl(${hue} 80% 50%)`; requestAnimationFrame(draw) }
draw()
</script></body></html>"""


class Drawing(Site):  # pylint: disable=too-few-public-methods
    """The fake site, with the drawing page at /drawing."""

    def answer(self, url: str, method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        if url.split("?")[0] == f"{SITE}/drawing":
            return 200, {"Content-Type": "text/html"}, DRAWING.encode()
        return super().answer(url, method, cookie)


def frames(sent: list[dict]) -> int:
    return sum(1 for message in sent if message["type"] == "frame")


@needs_chromium
def test_the_screencast_sends_every_second_frame_and_none_while_the_page_is_hidden():
    async def scenario():
        driver, recorder = await recording(Drawing())
        try:
            started: list[dict] = []
            while recorder._cdp is None or not recorder._casting:  # the first page's, set up meanwhile
                await asyncio.sleep(0.05)
            sending = recorder._cdp.send

            async def spy(method, params=None):
                if method == "Page.startScreencast":
                    started.append(params)
                return await sending(method, params)

            recorder._cdp.send = spy  # type: ignore[method-assign]
            await recorder.handle({"type": "goto", "url": f"{SITE}/drawing"})
            await asyncio.sleep(1)
            shown = frames(driver.sent)
            await recorder.handle({"type": "screen", "visible": False})
            await asyncio.sleep(0.3)  # a frame on its way when it stopped
            hidden = frames(driver.sent)
            await asyncio.sleep(1)
            still_hidden = frames(driver.sent) - hidden
            # A load while hidden starts nothing (_loaded); a resize neither.
            await recorder.handle({"type": "reload"})
            await recorder.handle({"type": "viewport", "width": 1000, "height": 700})
            await asyncio.sleep(0.5)
            after_load = frames(driver.sent) - hidden
            await recorder.handle({"type": "screen", "visible": True})
            await asyncio.sleep(1)
            again = frames(driver.sent) - hidden
            return started, shown, still_hidden, after_load, again
        finally:
            await recorder.close()

    started, shown, still_hidden, after_load, again = asyncio.run(scenario())
    assert shown > 3, "the drawing page's frames"
    assert still_hidden == 0 and after_load == 0, "no frames while the page is hidden"
    assert again > 3, "frames again once it shows"
    assert started and all(params["everyNthFrame"] == recorder_module.EVERY_NTH_FRAME == 2 for params in started)


@needs_chromium
def test_frames_go_as_binary_messages_where_the_socket_takes_them():
    async def scenario():
        driver, recorder = await recording(Drawing())
        frames_sent: list[bytes] = []

        async def send_bytes(data: bytes) -> None:
            frames_sent.append(data)

        recorder.send_bytes = send_bytes
        try:
            await recorder.handle({"type": "goto", "url": f"{SITE}/drawing"})
            await asyncio.sleep(0.5)
            return frames_sent, frames(driver.sent[-20:])
        finally:
            await recorder.close()

    sent, json_frames = asyncio.run(scenario())
    assert sent and all(frame[:2] == b"\xff\xd8" for frame in sent), "the JPEG itself"
    assert json_frames == 0, "none as JSON besides"
