#!/usr/bin/env python3
"""Record the actual SNN/ZO node, never a seeded or simulated dashboard.

Requires Python Playwright, its Chromium browser, and ffmpeg on PATH.
Run: python tools/record_demo.py
The URL and passcode are prompted without echo (or read from ARUI_DEMO_URL
and ARUI_DEMO_PASSCODE). Authentication is excluded from the recording.
Review both outputs before committing: terminal output can contain secrets.
The script visits views only; it never starts/stops research or paper mode.
"""
import argparse
import getpass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


def convert(source, output, offset):
    mp4 = output / 'demo.mp4'
    subprocess.run([
        'ffmpeg', '-v', 'error', '-y', '-ss', str(offset), '-i', str(source),
        '-t', '72', '-an', '-vf', 'scale=1200:-2,fps=12', '-c:v', 'libx264',
        '-crf', '23', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(mp4),
    ], check=True)
    gif = output / 'demo.gif'
    for width, colors in [(1200, 128), (960, 128), (960, 64), (960, 32)]:
        filters = (
            f'fps=12,scale={width}:-2:flags=lanczos,split[a][b];'
            f'[a]palettegen=max_colors={colors}:stats_mode=diff[p];'
            '[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle'
        )
        subprocess.run([
            'ffmpeg', '-v', 'error', '-y', '-i', str(mp4), '-filter_complex',
            filters, '-loop', '0', str(gif),
        ], check=True)
        if gif.stat().st_size <= 8_000_000:
            return
    gif.unlink()
    raise RuntimeError('GIF exceeds 8 MB; MP4 retained for manual encoding.')


def record(output, url, passcode, headed):
    from playwright.sync_api import sync_playwright

    def dwell(page, seconds=3):
        page.wait_for_timeout(seconds * 1000)

    def click(page, selector):
        target = page.locator(selector).first
        target.wait_for(state='visible')
        rect = target.bounding_box()
        page.mouse.move(rect['x'] + rect['width']/2,
                        rect['y'] + rect['height']/2, steps=24)
        dwell(page, 0.4)
        target.click()

    def navigate(page, label):
        click(page, '.burger')
        dwell(page, 0.7)
        click(page, f'.menu-item:has-text("{label}")')
        dwell(page)

    with tempfile.TemporaryDirectory(prefix='arui-demo-') as tmp:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            login = browser.new_context()
            page = login.new_page()
            page.goto(url + '/dashboard', wait_until='domcontentloaded')
            page.wait_for_function(
                'document.querySelector("#login-pc") || document.querySelector(".burger")')
            if page.locator('#login-pc').count():
                page.locator('#login-pc').fill(passcode)
                page.locator('#login-go').click()
            page.locator('.burger').wait_for(timeout=30000)
            # Cookies remain in memory; no credential-bearing storage-state file.
            state = login.storage_state()
            await_data = """() => typeof S !== 'undefined' && S.runs.length > 1
                && S.runs.some(r => r.metric != null) && S.gpus.length > 0"""
            page.wait_for_function(await_data, timeout=30000)
            is_snn = page.evaluate("""() => /spik|\\bsnn\\b/i.test(JSON.stringify(S))
                && /zero.order|zeroth.order|\\bzo\\b/i.test(JSON.stringify(S))""")
            if not is_snn:
                raise RuntimeError('Live state does not identify a populated SNN/ZO run.')
            context = browser.new_context(
                storage_state=state, viewport={'width': 1200, 'height': 800},
                record_video_dir=tmp, record_video_size={'width': 1200, 'height': 800})
            await_start = time.monotonic()
            page = context.new_page()
            # Block control mutations throughout filming; GET polls and terminal
            # WebSockets remain live. Login happened in the unrecorded context.
            context.route('**/api/**', lambda route: route.continue_()
                          if route.request.method in ('GET', 'HEAD', 'OPTIONS')
                          else route.abort())
            page.goto(url + '/dashboard', wait_until='domcontentloaded')
            page.wait_for_function(await_data, timeout=30000)
            page.locator('#cw canvas').first.wait_for()
            dwell(page)
            offset = time.monotonic() - await_start
            start = time.monotonic()
            dwell(page, 18)
            navigate(page, 'Analysis')
            canvas = page.locator('.anav2-grid canvas').first
            canvas.wait_for()
            rect = canvas.bounding_box()
            for fraction in (0.2, 0.4, 0.6, 0.8):
                page.mouse.move(rect['x'] + rect['width'] * fraction,
                                rect['y'] + rect['height'] * 0.5, steps=30)
                dwell(page)
            # Scoping has no navigation item in the current UI. Invoke its
            # existing renderer; it fetches actual /scope/status without restart.
            page.evaluate('showScopingModal()')
            page.locator('.scope-wrap').wait_for()
            dwell(page, 9)
            page.goto(url + '/write-paper', wait_until='domcontentloaded')
            page.locator('#paper-tab-body').wait_for()
            dwell(page, 10)
            navigate(page, 'Dashboard')
            page.mouse.move(1150, 750, steps=24)
            dwell(page, max(3, 72 - (time.monotonic() - start)))
            video = page.video
            context.close()
            source = Path(video.path())
            login.close()
            browser.close()
        convert(source, output, offset)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'docs')
    parser.add_argument('--headed', action='store_true')
    args = parser.parse_args()
    if not shutil.which('ffmpeg'):
        parser.error('ffmpeg is required; no recording was made.')
    url = (os.environ.get('ARUI_DEMO_URL') or getpass.getpass('Live node URL: ')).rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username:
        parser.error('Enter an HTTP(S) node URL without embedded credentials.')
    passcode = os.environ.get('ARUI_DEMO_PASSCODE') or getpass.getpass('Passcode: ')
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        record(args.output, url, passcode, args.headed)
    except Exception as error:
        # Playwright exceptions include URLs; suppress them to protect the node.
        print(f'Recording stopped ({type(error).__name__}). No acceptance claim made.', file=sys.stderr)
        return 1
    print('Created demo.gif and demo.mp4. Review for visible secrets before committing.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
