"""Headless-Chrome journey over the Chrome DevTools protocol against a running DUA backend and frontend.

Not collected by pytest. Used for M12 criterion 11:
- ``unavailable``: no Docker on the machine; the compatibility check reports Resolver unavailable and
  approval stays locked (development machine);
- ``compatible``: Docker available; the real resolver verifies the plan, then approval, validation and
  export proceed and the ZIP downloads (CI Docker job).

Usage: python tests/e2e/browser_journey.py <mode> <out_dir> <width> <height> [devtools_port]
Prerequisites: backend on 127.0.0.1:8000, frontend on localhost:5173, Chrome with
--remote-debugging-port. Requires the ``websockets`` package (test tooling only).
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
import urllib.request

import websockets

MODE, OUT, W, H = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
PORT = int(sys.argv[5]) if len(sys.argv) > 5 else 9222
MANIFEST = "requests==2.28.0\nurllib3==1.26.5\n"


class Page:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def send(self, method, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    async def js(self, expr):
        r = await self.send("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(json.dumps(r["exceptionDetails"])[:500])
        return r["result"].get("value")

    def q(self, testid):
        return f"document.querySelector('[data-testid=\"{testid}\"]')"

    async def wait(self, testid, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if await self.js(f"!!{self.q(testid)}"):
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(f"{testid} did not appear within {timeout}s")

    async def text(self, testid):
        return await self.js(f"{self.q(testid)}?.textContent ?? null")

    async def click(self, testid, timeout=60):
        await self.wait(testid, timeout)
        await self.js(f"{self.q(testid)}.scrollIntoView({{block:'center'}}); {self.q(testid)}.click()")

    async def shot(self, name, testid=None):
        if testid:
            await self.js(f"{self.q(testid)}?.scrollIntoView({{block:'start'}})")
            await asyncio.sleep(0.6)
        data = (await self.send("Page.captureScreenshot", format="png"))["data"]
        with open(f"{OUT}/{name}-{W}.png", "wb") as fh:
            fh.write(base64.b64decode(data))


async def journey() -> dict:
    tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json").read())
    url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
    result: dict = {"mode": MODE, "width": W}
    async with websockets.connect(url, max_size=50_000_000) as ws:
        p = Page(ws)
        await p.send("Page.enable")
        await p.send("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=W < 600)
        await p.send("Page.navigate", url="http://localhost:5173/#/analyze")
        await p.click("source-manifest")
        await p.wait("raw-content-input")
        await p.js("""(() => { const t = document.querySelector('[data-testid="raw-content-input"]');
            const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
            set.call(t, %s); t.dispatchEvent(new Event('input', {bubbles: true})); })()""" % json.dumps(MANIFEST))
        await p.click("btn-analyze")
        await p.click("btn-generate-recommendations", 180)
        await p.click("btn-generate-plan", 180)
        await p.wait("compatibility-panel", 120)
        result["initial_status"] = await p.text("compat-status")
        result["initial_locked"] = await p.js(f"!!{p.q('approval-locked')} && !{p.q('confirm-approval-btn')}")
        await p.shot("1-not-verified", "compatibility-panel")
        await p.click("compat-start")
        if MODE == "unavailable":
            await p.wait("compat-unavailable", 180)
            result["status"] = await p.text("compat-status")
            result["locked"] = await p.js(f"!!{p.q('approval-locked')} && !{p.q('confirm-approval-btn')}")
            await p.shot("2-resolver-unavailable", "compatibility-panel")
        else:
            await p.wait("compat-progress", 30)
            result["progress"] = await p.text("compat-progress")
            await p.wait("compat-compatible", 420)
            result["status"] = await p.text("compat-status")
            result["disclosure"] = await p.text("compat-disclosure")
            result["introduced"] = await p.text("compat-introduced")
            await p.js(f"{p.q('compat-proposed-table')}.open = true")
            result["dates"] = await p.text("compat-proposed-table")
            await p.shot("2-compatible", "compatibility-panel")
            await p.click("approval-checkbox")
            await p.click("confirm-approval-btn")
            await p.wait("approval-confirmed-banner", 60)
            await p.click("btn-run-validation", 60)
            await p.wait("validation-status-badge", 400)
            result["validation"] = await p.text("validation-status-badge")
            await p.shot("3-validation", "sandbox-validation-section")
            await p.click("generate-export-button", 120)
            await p.wait("export-results-card", 120)
            result["download_status"] = await p.js("""(async () => {
                const a = [...document.querySelectorAll('[data-testid="export-results-card"] a')]
                  .find(x => x.href.includes('bundle_zip'));
                return a ? (await fetch(a.href)).status : null; })()""")
            await p.shot("4-export", "export-deliverables-section")
        result["overflow"] = await p.js("document.documentElement.scrollWidth - window.innerWidth")
    return result


def main() -> int:
    result = asyncio.run(journey())
    print(json.dumps(result))
    ok = result["initial_locked"] and result["overflow"] <= 0 and "Not verified" in (result["initial_status"] or "")
    if MODE == "unavailable":
        ok = ok and result["status"] == "Resolver unavailable" and result["locked"]
    else:
        ok = (ok and result["status"] == "Compatible" and "does not mean" in (result["disclosure"] or "")
              and result["download_status"] == 200)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
