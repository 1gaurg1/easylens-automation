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
import re
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright
from PIL import Image, ImageOps
from pydantic import BaseModel

load_dotenv(Path(__file__).parent / ".env")  # picks up ANTHROPIC_API_KEY

# --- Config -----------------------------------------------------------------

EASY_LENS_URL = "https://easylens.snapchat.com"
PROFILE_DIR = Path(__file__).parent / ".browser-profile"  # persistent login lives here
LOG_FILE = Path(__file__).parent / "runs.jsonl"
MODEL = "claude-haiku-5-5"

# Selectors: inspect the page and update these. Prefer roles/placeholders over
# CSS classes, which change often.
# Run with --inspect to print the real elements from your logged-in session.
PROMPT_INPUT = 'textarea:visible, [contenteditable="true"]:visible, [role="textbox"]:visible, input[type="text"]:visible'
SUBMIT_BUTTON = 'button[type="submit"]:visible, button[aria-label*="send" i]:visible, button[aria-label*="submit" i]:visible'
SUBMITTED_URL = "**/chat**"                                  # Easy Lens moves to /chat?checkpoint=... after a prompt
REPLYING_TEXT = re.compile(r"is replying", re.I)             # "Easy Lens is replying..." while it works
CONFIRM_TEXT = re.compile(r"^\s*(submit|continue|confirm|send|done|next)\s*$", re.I)
PUBLISH_BUTTON = re.compile(r"publish lens", re.I)
# Buttons clicked through the publish dialog, incl. capturing the preview from the (fake) camera.
PUBLISH_STEP = re.compile(r"^\s*(publish|submit|next|continue|confirm|done|capture|take (a )?(photo|snapshot|picture)|use (this )?(photo|snapshot|capture))\b", re.I)
PUBLISHED_URL = re.compile(r"/publish/published")
CREATE_ANOTHER = re.compile(r"create another lens", re.I)
PUBLISH_TIMEOUT_S = 3 * 60
CHALLENGE_SIGNALS = ["iframe[src*='captcha']", "text=/verify you are human/i", "text=/too many requests/i"]

MIN_DELAY_S, MAX_DELAY_S = 60, 120   # be conservative; you're on someone else's service
GENERATION_TIMEOUT_S = 10 * 60
IDLE_DONE_S = 10                     # no "replying" and no questions for this long = finished
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
    img = ImageOps.exif_transpose(Image.open(image_path)).convert("RGB")  # phone photos: honour rotation tag
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


# Finds multiple-choice questions Easy Lens asks in the chat column (above the
# prompt box, same width). Options are groups of 2+ short text buttons sharing
# a container; each option gets a data-el-choice id so Python can click it.
CHOICES_JS = r"""() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const box = [...document.querySelectorAll('textarea, [contenteditable="true"], [role=textbox], input[type=text]')].find(vis);
  if (!box) return [];
  const b = box.getBoundingClientRect();
  const skip = /^(plan|tools|agent|create|explore|preview|edit|copy|retry|skip|show (more|less)|submit|continue|confirm|send|done|next)$|\bsteps?$|^other\b|your own|custom/i;
  const cands = [...document.querySelectorAll('button, [role=button], [role=radio], [role=option], [role=checkbox], label')]
    .filter(el => {
      if (!vis(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
      const r = el.getBoundingClientRect();
      if (r.bottom > b.top - 5 || r.right < b.left - 40 || r.left > b.right + 40) return false;
      const t = (el.innerText || '').trim();
      return t.length > 0 && t.length <= 120 && !skip.test(t);
    });
  const leaves = cands.filter(el => !cands.some(o => o !== el && el.contains(o)));
  const groups = new Map();
  for (const el of leaves) {
    let p = el.parentElement;  // nearest container (up to 3 levels) holding another option
    for (let i = 0; i < 3 && p && !leaves.some(o => o !== el && p.contains(o)); i++) p = p.parentElement;
    if (!groups.has(p)) groups.set(p, []);
    groups.get(p).push(el);
  }
  const out = []; let n = 0;
  for (const els of groups.values()) {
    if (els.length < 2) continue;
    out.push(els.map(el => {
      const id = 'c' + Date.now() + '-' + (n++);
      el.setAttribute('data-el-choice', id);
      return {id, text: el.innerText.trim().replace(/\s+/g, ' ').slice(0, 80)};
    }));
  }
  return out;
}"""


def visible_buttons(page: Page, name: re.Pattern) -> list:
    return [b for b in page.get_by_role("button", name=name).all() if b.is_visible()]


def answer_choices(page: Page, answered: set[frozenset[str]]) -> bool:
    """If Easy Lens is asking multiple-choice questions, pick a random option for each."""
    groups = [g for g in page.evaluate(CHOICES_JS) if frozenset(c["text"] for c in g) not in answered]
    if not groups:
        return False
    for group in groups:
        pick = random.choice(group)
        print(f"  Easy Lens asked a question - picked {pick['text']!r} (of {len(group)} options)")
        page.locator(f'[data-el-choice="{pick["id"]}"]').click()
        answered.add(frozenset(c["text"] for c in group))
        time.sleep(random.uniform(1, 2))
    confirm = visible_buttons(page, CONFIRM_TEXT)
    if confirm:
        confirm[-1].click()
    return True


def wait_until_done(page: Page) -> bool:
    """Answer any questions, and return once Easy Lens has stopped replying."""
    start = time.monotonic()
    deadline = start + GENERATION_TIMEOUT_S
    answered: set[frozenset[str]] = set()
    seen_reply, idle_since = False, None
    while time.monotonic() < deadline:
        wait_for_human_if_challenged(page)
        if answer_choices(page, answered):
            seen_reply, idle_since = True, None  # it already replied (with questions)
            time.sleep(5)
            continue
        if any(el.is_visible() for el in page.get_by_text(REPLYING_TEXT).all()):
            seen_reply, idle_since = True, None
        elif seen_reply or time.monotonic() - start > 60:  # don't hang if the "replying" text was missed
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since >= IDLE_DONE_S:
                return True
        time.sleep(2)
    return False


def publish_lens(page: Page) -> bool:
    """Click Publish Lens, then step through the dialog (capturing the camera
    preview - your --camera-image - when asked) until the 'published' screen."""
    start = visible_buttons(page, PUBLISH_BUTTON)
    if not start:
        print("  [!] No 'Publish Lens' button found.")
        return False
    start[0].click()
    deadline = time.monotonic() + PUBLISH_TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(3)
        if PUBLISHED_URL.search(page.url) or visible_buttons(page, CREATE_ANOTHER):
            return True
        wait_for_human_if_challenged(page)
        steps = [b for b in visible_buttons(page, PUBLISH_STEP) if b.is_enabled()]
        if steps:
            step = steps[-1]  # dialogs sit at the end of the page, primary action last
            print(f"  publish: clicking {(step.inner_text().strip() or step.get_attribute('aria-label'))!r}")
            step.click()
    print("  [!] Publishing didn't finish - complete it in the browser, and run --inspect on the stuck screen.")
    return False


def submit_prompt(page: Page, prompt: str) -> bool:
    wait_for_human_if_challenged(page)
    # Each prompt gets a fresh lens, not an edit of the last one.
    another = visible_buttons(page, CREATE_ANOTHER)
    if another:
        another[0].click()
        page.wait_for_load_state()
    elif "/chat" in page.url:
        page.goto(EASY_LENS_URL)

    box = page.locator(PROMPT_INPUT).first
    box.wait_for(state="visible", timeout=30_000)
    box.click()
    box.fill("")
    box.press_sequentially(prompt, delay=random.randint(30, 80))  # readable pace, easy to watch

    # Chat-style box: Enter usually sends. Fall back to clicking a send button.
    box.press("Enter")
    try:
        page.wait_for_url(SUBMITTED_URL, timeout=10_000)
    except PWTimeout:
        send = page.locator(SUBMIT_BUTTON).first
        if send.count():
            send.click()
        else:
            print("  [!] Couldn't find the send button - click it yourself (run --inspect to fix).")

    return wait_until_done(page)


INSPECT_JS = """() => {
  const vis = el => el.offsetParent !== null || el.getClientRects().length > 0;
  const short = el => el.outerHTML.replace(/<svg[\\s\\S]*?<\\/svg>/g, '<svg/>').slice(0, 300);
  const out = ['URL: ' + location.href, '', 'TEXT BOXES:'];
  const boxes = [...document.querySelectorAll('textarea, input, [contenteditable="true"], [role=textbox]')];
  for (const el of boxes) out.push((vis(el) ? '  [visible] ' : '  [hidden]  ') + short(el));
  const box = boxes.find(vis);
  let area = box; for (let i = 0; i < 6 && area && area.parentElement; i++) area = area.parentElement;
  out.push('', 'BUTTONS NEAR THE PROMPT BOX:');
  for (const b of (area || document).querySelectorAll('button')) if (vis(b)) out.push('  ' + short(b));
  if (box) {
    const r0 = box.getBoundingClientRect();
    out.push('', 'CLICKABLE ITEMS IN THE CHAT ABOVE THE PROMPT BOX:');
    for (const el of document.querySelectorAll('button, [role=button], [role=radio], [role=option], [role=checkbox], label')) {
      const r = el.getBoundingClientRect();
      if (vis(el) && r.bottom <= r0.top && r.right >= r0.left - 40 && r.left <= r0.right + 40) out.push('  ' + short(el));
    }
  }
  out.push('', 'ALL VISIBLE BUTTONS (text | aria-label):');
  for (const b of document.querySelectorAll('button, [role=button]'))
    if (vis(b)) out.push('  ' + (b.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 60) + ' | ' + (b.getAttribute('aria-label') || ''));
  return out.join('\\n');
}"""


def inspect_page(page: Page) -> None:
    while True:
        print("\n" + page.evaluate(INSPECT_JS))
        groups = page.evaluate(CHOICES_JS)
        print("\nQUESTIONS THE SCRIPT WOULD ANSWER:")
        for g in groups:
            print("  - " + " | ".join(c["text"] for c in g))
        if not groups:
            print("  (none)")
        if input("\nPress Enter to inspect again, or type q then Enter to quit: ").strip().lower() == "q":
            return


def log_result(prompt: str, ok: bool, published: bool, url: str) -> None:
    with LOG_FILE.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "prompt": prompt, "ok": ok, "published": published, "url": url}) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--theme", default="")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true", help="only print generated prompts")
    parser.add_argument("--camera-image", type=Path, help="still image to use as the webcam for lens previews")
    parser.add_argument("--inspect", action="store_true", help="print the page's text boxes and buttons, then exit")
    parser.add_argument("--no-publish", action="store_true", help="generate lenses but don't publish them")
    args = parser.parse_args()
    if not args.inspect and not args.theme:
        parser.error("--theme is required (unless using --inspect)")

    prompts = [] if args.inspect else generate_prompts(anthropic.Anthropic(), args.theme, args.count)
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
        try:  # assumes the saved profile is already logged in
            page.locator(PROMPT_INPUT).first.wait_for(state="visible", timeout=60_000)
        except PWTimeout:
            print("\n[!] Easy Lens prompt box never appeared - is this profile still logged in?")
            if not args.inspect:
                ctx.close()
                return

        if args.inspect:
            inspect_page(page)
            ctx.close()
            return

        failures = 0
        for i, prompt in enumerate(prompts, 1):
            print(f"\n[{i}/{len(prompts)}] {prompt}")
            ok = submit_prompt(page, prompt)
            print("  generated" if ok else "  timed out")
            published = ok and not args.no_publish and publish_lens(page)
            if published:
                print("  published")
            log_result(prompt, ok, published, page.url)

            failures = 0 if ok else failures + 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                print("Too many failures in a row; stopping.")
                break

            if i < len(prompts):
                delay = random.uniform(MIN_DELAY_S, MAX_DELAY_S)
                print(f"  waiting {delay:.0f}s")
                time.sleep(delay)

        input("\nFinished. Press Enter to close the browser...")
        ctx.close()


if __name__ == "__main__":
    main()
