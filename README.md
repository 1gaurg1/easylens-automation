# Easy Lens Loop

> **Status: shelved.** The test account got locked by Snapchat while this was in testing.
> I'm not pursuing it further - see [Why it's shelved](#why-its-shelved).

## Intention

[Snapchat Easy Lens](https://easylens.snapchat.com) turns a short text prompt into an
AR lens. The idea was to put an LLM in the loop so lenses could be generated hands-free:

1. **Claude writes the prompts.** Given a theme (e.g. "retro 80s arcade"), Claude Haiku 5.5
   generates a batch of distinct, concrete lens descriptions.
2. **A browser types them in.** Playwright drives a logged-in Chromium session, submits
   each prompt to Easy Lens, and waits for the lens to build.
3. **Hands-off from prompt to published lens.** The script answers Easy Lens's
   follow-up multiple-choice questions at random, previews the lens on a still photo
   (fed in as a fake webcam), publishes it, clicks "Create Another Lens", and moves on
   to the next prompt.

Easy Lens has no public API, so driving the web UI was the only route.

## What got built

`easylens_loop.py` does all of the above:

| Piece | How |
|---|---|
| Prompt generation | Anthropic SDK, structured output into a list of prompts |
| Persistent login | Playwright persistent context in `.browser-profile/` |
| Still-image camera | `--camera-image face.jpg` converts the image to a one-frame Y4M and feeds it to Chromium's fake video capture |
| Easy Lens questions | Detects groups of option buttons in the chat column and picks one at random |
| Completion detection | Waits for "Easy Lens is replying…" to go away |
| Publishing | Clicks through the publish dialog until the "submitted successfully" page |
| Debugging | `--inspect` dumps the page's text boxes and buttons to find selectors |
| Logging | Each run appended to `runs.jsonl` |

Verified: prompt generation, the fake camera feed (on the real Easy Lens site), and
the question → publish → next-lens flow against a mock page. On the real site it
submitted prompts and generated lenses; the automated publish step never got a clean
run before the account was locked.

## Why it's shelved

Snapchat locked the account after a few automated runs, and Google refuses sign-ins
from automated browsers. Making this work reliably would mean fighting Snap's bot
detection: evading it, getting accounts locked, rotating through burner accounts. That's
a losing game, it's against Snap's Terms of Service, and it isn't something I want to do.

Lessons:

- **No API means no sanctioned automation.** Driving a consumer web app with a bot puts
  you up against its anti-abuse systems, not just fiddly selectors.
- **Don't test on an account you care about.** The lock came fast.
- **The LLM half is the safe, useful half.** Generating good lens prompts works fine on
  its own.

## If I pick it back up

- Use only the prompt generator (`--dry-run`) and make and publish lenses by hand.
- Look at [Lens Studio](https://ar.snap.com/lens-studio), Snap's official desktop tool,
  for building lenses in volume.
- Check whether Snap has added an official API or creator tooling for Easy Lens.

## Running the safe part

```bash
pip install -r requirements.txt
# put ANTHROPIC_API_KEY=... in .env
python3 easylens_loop.py --theme "retro 80s arcade" --count 5 --dry-run
```

`--dry-run` only calls Claude and prints the prompts; it never opens a browser or
touches Snapchat.
