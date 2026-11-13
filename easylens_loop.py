"""
Easy Lens prompt loop: Claude generates AR lens prompts, Playwright types them
into the Easy Lens web UI in your own logged-in browser session.

Deliberately NOT included: stealth plugins, fingerprint spoofing, CAPTCHA
solving, or any attempt to evade Snap's rate limits / bot detection. The
script runs headed, at a slow human pace, and pauses for you if Snap shows
a challenge or throttles you. Automating Snap's UI may still be against
their Terms of Service - use a secondary account and keep volumes low.

Setup:
    pip install -r requirements.txt
    playwright install chromium
    put ANTHROPIC_API_KEY in .env

Run:
    python easylens_loop.py --theme "retro 80s arcade" --count 5
    python easylens_loop.py --theme "..." --camera-image face.jpg   # still image as webcam
    (first run: log in manually in the opened window, then press Enter)
"""

import argparse
import json
import random
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright
from PIL import Image
from pydantic import BaseModel

load_dotenv(Path(__file__).parent / ".env")  # picks up ANTHROPIC_API_KEY

# --- Config -----------------------------------------------------------------

EASY_LENS_URL = "https://easylens.snapchat.com"
PROFILE_DIR = Path(__file__).parent / ".browser-profile"  # persistent login lives here
LOG_FILE = Path(__file__).parent / "runs.jsonl"
MODEL = "claude-haiku-5-5"

# Selectors: inspect the page and update these. Prefer roles/placeholders over
# CSS classes, which change often.
PROMPT_INPUT = "textarea"                                    # e.g. 'textarea[placeholder*="Describe"]'
SUBMIT_BUTTON = "role=button[name=/generate|create|send/i]"
DONE_SIGNAL = "role=button[name=/publish|save/i]"            # element that appears when generation finishes
CHALLENGE_SIGNALS = ["iframe[src*='captcha']", "text=/verify you are human/i", "text=/too many requests/i"]

MIN_DELAY_S, MAX_DELAY_S = 60, 120   # be conservative; you're on someone else's service
GENERATION_TIMEOUT_MS = 5 * 60_000
MAX_CONSECUTIVE_FAILURES = 3

# --- Claude: prompt generation ---------------------------------------------


class LensPrompts(BaseModel):
    prompts: list[str]


SYSTEM = (
    "You write prompts for Snapchat Easy Lens, a tool that turns a short natural-"
    "language description into an AR face/world lens. Each prompt should be one or "
    "two sentences, concrete and visual (what appears, where on the face or scene, "
    "colors, motion, style). Keep them distinct from each other and family-friendly."
)


def generate_prompts(client: anthropic.Anthropic, theme: str, count: int) -> list[str]:
    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Write {count} Easy Lens prompts on the theme: {theme}"}],
        output_format=LensPrompts,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined: {response.stop_details}")
    return response.parsed_output.prompts[:count]


# --- Browser automation -----------------------------------------------------


def image_to_y4m(image_path: Path, out_path: Path, max_width: int = 1280) -> Path:
    """Convert a still image into a one-frame Y4M video, the format Chromium's
    fake webcam (--use-file-for-fake-video-capture) reads. Chromium loops it."""
    img = Image.open(image_path).convert("RGB")
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)))
    img = img.crop((0, 0, img.width - img.width % 2, img.height - img.height % 2))  # 4:2:0 needs even dims
    y, cb, cr = img.convert("YCbCr").split()
    half = (img.width // 2, img.height // 2)
    with out_path.open("wb") as f:
        f.write(f"YUV4MPEG2 W{img.width} H{img.height} F30:1 Ip A1:1 C420jpeg\n".encode())
        f.write(b"FRAME\n" + y.tobytes() + cb.resize(half).tobytes() + cr.resize(half).tobytes())
    return out_path


def launch_args(camera_image: Path | None) -> list[str]:
    if camera_image is None:
        return []
    y4m = image_to_y4m(camera_image, Path(__file__).parent / ".camera.y4m")
    return [
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-video-capture={y4m}",
    ]


def challenge_present(page: Page) -> bool:
    return any(page.locator(sel).count() > 0 for sel in CHALLENGE_SIGNALS)


def wait_for_human_if_challenged(page: Page) -> None:
    """If Snap shows a CAPTCHA / rate-limit page, stop and let the human handle it."""
    if challenge_present(page):
        print("\n[!] Snap is showing a verification or rate-limit page.")
        print("    Resolve it yourself in the browser (or wait it out), then press Enter.")
        print("    If this keeps happening, lower --count or raise the delays.")
        input()


def submit_prompt(page: Page, prompt: str) -> bool:
    wait_for_human_if_challenged(page)

    box = page.locator(PROMPT_INPUT).first
    box.wait_for(state="visible", timeout=30_000)
    box.click()
    box.fill("")
    box.type(prompt, delay=random.randint(30, 80))  # readable pace, easy to watch
    page.locator(SUBMIT_BUTTON).first.click()

    try:
        page.locator(DONE_SIGNAL).first.wait_for(state="visible", timeout=GENERATION_TIMEOUT_MS)
        return True
    except PWTimeout:
        wait_for_human_if_challenged(page)
        return False


def log_result(prompt: str, ok: bool) -> None:
    with LOG_FILE.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "prompt": prompt, "ok": ok}) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--theme", required=True)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true", help="only print generated prompts")
    parser.add_argument("--camera-image", type=Path, help="still image to use as the webcam for lens previews")
    args = parser.parse_args()

    prompts = generate_prompts(anthropic.Anthropic(), args.theme, args.count)
    for i, p in enumerate(prompts, 1):
        print(f"{i}. {p}")
    if args.dry_run:
        return

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            args=launch_args(args.camera_image),
            # Snap asks for camera AND mic together; with a fake camera, the mic is fake too.
            permissions=["camera", "microphone"] if args.camera_image else [],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(EASY_LENS_URL)
        input("\nLog in if needed and open the Easy Lens editor, then press Enter to start...")

        failures = 0
        for i, prompt in enumerate(prompts, 1):
            print(f"\n[{i}/{len(prompts)}] {prompt}")
            ok = submit_prompt(page, prompt)
            log_result(prompt, ok)
            print("  done" if ok else "  timed out")

            failures = 0 if ok else failures + 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                print("Too many failures in a row; stopping.")
                break

            if i < len(prompts):
                # Before the next prompt, save/publish the lens yourself if you want to keep it.
                delay = random.uniform(MIN_DELAY_S, MAX_DELAY_S)
                print(f"  waiting {delay:.0f}s")
                time.sleep(delay)

        input("\nFinished. Press Enter to close the browser...")
        ctx.close()


if __name__ == "__main__":
    main()
