"""Render index.html frame by frame (renderAt(t) is deterministic) and save PNGs.

    uvx --from playwright python assets/motion/capture.py /tmp/frames
    ffmpeg -framerate 30 -i /tmp/frames/f%04d.png -vf "fps=15,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" assets/demo.gif

Open index.html in a browser to preview the loop live.
"""
import asyncio, sys, pathlib
from playwright.async_api import async_playwright

HERE = pathlib.Path(__file__).parent
OUT = pathlib.Path(sys.argv[1])
FPS = int(sys.argv[2]) if len(sys.argv) > 2 else 30
ONLY = [float(x) for x in sys.argv[3].split(",")] if len(sys.argv) > 3 else None

async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
        await page.goto((HERE / "index.html").as_uri() + "?capture")
        await page.evaluate("document.fonts.ready")
        await page.wait_for_timeout(800)
        duration = await page.evaluate("window.DURATION")
        times = ONLY or [i / FPS for i in range(int(duration * FPS))]
        for i, t in enumerate(times):
            await page.evaluate(f"renderAt({t})")
            await page.screenshot(path=str(OUT / f"f{i:04d}.png"))
        await browser.close()

asyncio.run(main())
