# Deferred items and open questions

Everything here is deliberately unresolved or deferred at the brief stage — not because it was forgotten, but because deciding it before there's real data and a working collector would just be guessing. Finished work doesn't belong here — how the project actually ended up working gets documented separately as implementation proceeds (to appear in `docs/`) and in `CHANGELOG.md`.

---

## Closed questions (kept for history)

- **Where the agent lives and how it prints.** The main home PC, running the whole pipeline in one pass and printing over the local network to an HP Color LaserJet Pro MFP M377dw (AirPrint/CUPS) — cloud printing is physically unavailable for this model; HP shut down ePrint in January 2023.
- **How it's launched.** On user request — a "Assemble newspaper" button in the local console (the same run is also available from the terminal). Originally a schedule was planned (a BIOS RTC alarm/Wake-on-LAN, auto-start, auto-shutdown) — reconsidered before implementing Stage 5 in favor of button-triggered launch, see "Launch" in [project-brief.md](project-brief.md). RTC/WoL reliability and the PC-wake infrastructure that came with it thereby dropped out as a concern — they're simply no longer part of the architecture.
- **Channel access.** MTProto (Telethon) under a separate helper Telegram account set up specifically for this project — not the main personal account, and not the Bot API.
- **Rendering technology.** HTML/CSS + a headless browser (Playwright/Chromium) — for near-free multi-column layout and typography via CSS, at the cost of a heavier dependency than Pillow.
- **Raw post storage.** SQLite (`data/tg_newspaper.db`, outside the repo): a `posts` table (channel, message id, date, text, attachment flag and type) unique on (channel, message id) — re-collecting the same period doesn't create duplicates.
- **Collection window and catching up on missed runs.** The collection window is always a fixed last 24 hours from the moment the button is pressed, with no regard to when or whether the previous run succeeded. Catching up on missed runs is deliberately not done: if the user doesn't press the button for a day or longer, the next run still only grabs its own last 24 hours, rather than backfilling the gap. This also closes the open question of "what to print if several days have piled up" — there's no reason to accumulate them.
- **Multiple pages / a multi-page newspaper, then a fixed issue length.** Originally deferred as a "deferred feature" until real data existed (see below). Revisited twice during Stage 3: first, a run on 67 real news items showed that a single page clearly can't hold a typical day's volume, and layout started generating as many pages as needed, uncapped (67 news items → 20 portrait pages). That turned out to be wrong on the merits: a printed newspaper is a fixed-length issue, not an endless feed. The final decision — 4 landscape A4 pages (`pipeline.NEWSPAPER_MAX_PAGES`; 2026-10-14: raised to 6 pages (+1 A3 sheet)), and whatever doesn't fit isn't dropped based on raw length — it's **selected by importance**: an LLM importance score (1-5, the same batch call that decides is_news) determines what gets printed first, what gets shortened by the model to save space, and what gets dropped from the issue as filler (`pipeline.build_newspaper`, `classifier.shorten`). See Stage 3 items 4-5 in [implementation-plan.md](implementation-plan.md).
- **The cross-day deduplication window.** The same news story can resurface in a different channel not on the same day but days later (a delayed repost/retelling) — with collection limited to the last 24 hours (see above), there's no other way to catch that. Stage 2 deduplication compares the day's candidates not only against each other but against a rolling window of already-printed news from the last 5 days — using the same mechanism (a copy-paste heuristic plus LLM/embeddings), just with a wider comparison window. What's stored for this isn't the text of the finished page, but a separate list of published news items (headline/short text + date) — implemented in Stage 2.
- **Rule for choosing which version to keep on a duplicate (2026-10-14).** The most detailed version is kept; the LLM decides how detailed something is. The code already works this way: `_BEST_VERSION_INSTRUCTION` (copy-paste clusters) and `_PARAPHRASE_RESOLUTION_INSTRUCTION` (retellings) in `classifier.py` pick the post that covers the facts most fully (details, names, numbers, circumstances); length by itself isn't the criterion, and there's no channel priority.
- **News shortage (2026-10-14).** The `pipeline.MIN_ISSUE_NEWS` threshold is on the number of news items that passed into print (`included`): below it, no issue is assembled (`assemble_issue` returns None), and the user sees "Not enough news today: N (need at least M) — no issue assembled". The part about the printer being unreachable stayed in open questions.
- **Timezone and where the day boundary falls (2026-10-14).** The collection window stays the same — 24 hours from the button press; the issue date is the end-of-window date in the local timezone of the machine doing the layout. Time in the DB stays in UTC; the user sees local time.

Decisions and their rationale are in [project-brief.md](project-brief.md), under "Mechanics" and "Open forks".

## Open questions — to close before or during implementation

### Printer unavailability

What to do if the printer is unreachable at print time (powered off, out of paper, no network) — retry later within the same run, prompt the user to run it again, or just log it and skip printing. Belongs to Stage 4 (printing).

### Channel list format and editing

In the first version, the list is part of the configuration, with no UI. How it should be edited in the future (a text file edited by hand; a simple web UI) is undecided and doesn't need deciding before the first version.

---

## Deferred features

Not part of the first version — recorded so the idea isn't lost; no priority assigned.

> Four features that used to be listed here — importance personalization, section-based personalization, a channel-management web UI, and a printed archive — were, by the user's decision (2026-09-25), reclassified as part of the project's full, finished state rather than a deferred idea, and moved into [implementation-plan.md](implementation-plan.md) as Stages 6-8 (plus item 4 of Stage 4 for the archive). What remains here are only features that still aren't part of the finished state.

- **Filling the empty space at the bottom of a page.** Roughly 1/5 of a page's height is left empty at the bottom of every mosaic page: the next row doesn't fit in what's left and moves to the next page. Options: pick a smaller row from further in the queue to fill the gap, or shorten text further.
- **Bare links in post text.** They currently print as plain text (`rozetked.me/news/48845`), which is useless on paper. Either strip them or turn them into a short caption.
- **Migrating to gemini-embedding-2** (GA 2026-04-22) — requires recalibrating SIMILARITY_THRESHOLD (0.80, calibrated on gemini-embedding-001) against live data.
- **Robust retry logic for an offline printer** — with actual print retries, instead of just logging the failure.
- **Tracking paper usage** — a notification when paper or cartridges are running low.
- **Moving the agent off the main PC onto a separate device** — in case it becomes worth not tying up the main computer even for a short button-triggered run, and/or finding the cheapest board on the secondhand market (the project would be the device's only purpose, so no need to future-proof it with upgrades).
  - **Don't even consider the Raspberry Pi 1 / Zero (first generation) / Pi 2, at any price** — these are ARMv6/ARMv7 with no 64-bit OS support, and current Chromium packages are increasingly rarely built for these architectures; there's a real risk that the system Chromium package simply won't install there.
  - **Raspberry Pi 3 Model B/B+** — the practical minimum: ARMv8, runs 64-bit Raspberry Pi OS, 1 GB RAM is just enough for a once-a-day headless render (not under load, so speed isn't critical — a swap file as a safety margin in case of a heavier page). Sold cheaply and in bulk on the secondhand market as people upgrade to the 4/5. This is the answer to "the cheapest board that will definitely handle it".
  - **Raspberry Pi Zero 2 W** is technically also ARMv8/64-bit and cheaper than the Pi 3, but only has 512 MB RAM — noticeably higher risk of headless-browser instability during an unattended overnight run; not recommended as a way to save money here.
  - **Playwright doesn't ship its own Chromium binary for Linux ARM, in any form** — a plain `playwright install` simply won't work regardless of which board model you use. The working path is to install Chromium via the OS's own package (`apt install chromium-browser`) and point Playwright at it (`executable_path=...`) instead of trying to have it download the browser itself. This is a separate step to remember when migrating.
  - Booting from SSD/USB rather than an SD card, if you want insurance against wear from regular use — for a single dedicated purpose like this, a plain SD card is also acceptable.
