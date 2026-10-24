# Project brief: TG Newspaper

> An agent that prints a paper newspaper every morning, by itself, from yesterday's news on a chosen set of Telegram channels.

This document describes the idea and its mechanics at the start — before implementation began. As development proceeds, it is not meant to be rewritten: what actually got built is documented separately, in docs describing how the system actually works (to appear in `docs/` along the way — script architecture, storage schema) and in `CHANGELOG.md`. What's here is the intent and the decisions made before the first line of code.

---

## 1. Why

Reading the news means scrolling: the feed never ends, and whatever's actually important in it drowns among duplicates of the same story from ten different channels and clips with not a single word of text. The idea is to move news reading off the screen and onto paper: once a day, a single newspaper page is printed with what actually happened that day, with no duplicates and no clutter, and that's it for the day — no need to keep scrolling.

This isn't an aggregator in the usual sense (it doesn't serve a stream per request); it's an autonomous agent with a full cycle: it decides for itself what counted as news yesterday, lays it out itself, and prints it itself, with no human involved at any of these steps.

## 2. Product mechanics

### Channel list

Set manually, in the project's configuration — not auto-discovered, and not editable through a UI in the first version. Who maintains the list and how is an open question, see [future-development.md](future-development.md).

### Collecting posts

The source is MTProto under a user Telegram account (Telethon), not the Bot API. The account follows every source channel like an ordinary reader.

**The reason behind this choice, which shapes the architecture:** the agent needs to work on a "one short run on demand, no always-running process" model (see "Launch" below) — the Bot API has no "hand over a channel's history for a period" method: a bot only gets posts as they arrive (`getUpdates`/webhook), which requires a continuously running listener and is incompatible with that model. MTProto clients under a user account do have such a method (`iter_messages` with a date filter) — that became the deciding factor in favor of MTProto over the Bot API.

The practical consequence: the agent is **a single script**, not a pairing of "always-on collector + separate printer process". On every run it:
1. Asks Telegram for each source channel's history since the last successful run (some minimal state is kept somewhere — the timestamp of the last run, possibly the ID of the last message processed per channel).
2. Handles the rest of the processing itself from there — filtering, deduplication, rendering, printing.

If the PC didn't wake up on time, or a run failed with an error, that's not a disaster: the posts are still sitting in Telegram, the next run will take its own last 24 hours from the button press, and whatever fell outside that window just doesn't make it into an issue (catching up on what was missed is deliberately not implemented — see "Closed questions" in [future-development.md](future-development.md)). But the posts themselves are never lost, and losing one issue isn't losing data. That's the main practical advantage over the Bot API, where missing the collector's uptime window means permanently losing data.

The flip side: the Telethon session (the auth file) is the key to the Telegram account, not a token with limited rights like a bot's. It needs to be stored as a secret, not in the repository. That's why there's a **separate Telegram account for this, not the main personal one** — a leaked session file only compromises the helper account, which follows public news channels, not personal correspondence. The flip side of that decision is needing a separate phone number to register it (a number already registered to another account can't be reused) and a separate subscription for that account to every source channel.

### Filtering out "non-news"

Two-stage, with heuristics ahead of the LLM for predictability and to save on model calls:

**Heuristics** filter out what doesn't require understanding meaning:
- Media (photo/video) with no caption, or a caption under some word-count threshold (the original task description gave "a video with a 5-word caption" as an example)
- Posts with no text at all (voice messages, stickers, forwards with no comment)
- Other clear "not a text news item" signals — to be refined during implementation

**The LLM** decides what the heuristics left ambiguous:
- Whether a post is, by content, actually news (not an ad, not musing out loud, not an unanswered question to the chat)
- When needed, produces a condensed headline/summary for layout

The exact heuristic thresholds and the wording of the LLM classification prompt are implementation details, not part of the brief.

### Deduplication

The same news story usually spreads across several source channels: nearly verbatim (a repost/copy-paste) or retold in different words. A combination:
- A text-similarity heuristic filters out obvious copy-pastes and reposts without analyzing meaning
- LLM/embeddings — for cases where the same story is retold in different words across different channels

What exactly is kept when a duplicate is found was decided on 2026-10-14: the most detailed version, with the LLM judging how detailed it is (not the earliest by time, and not from the highest-priority channel) — see "Closed questions" in [future-development.md](future-development.md).

### Layout and rendering

From the filtered, deduplicated list of the day's news, an image is assembled in the style of a newspaper page: a headline of the day, several stories, columns, decorative elements (vignettes, rules — things that read visually as "a newspaper", not as a list of text).

Rendering is **HTML/CSS + a headless browser (Playwright/Chromium)**, not programmatic drawing via Pillow. The deciding factor: CSS multi-column layout (`column-count`) gives automatic text reflow between columns — exactly the newspaper-like behavior that would have to be hand-built in Pillow — and typography (hyphenation, drop caps, decorative borders, shadows) comes nearly for free there too. The cost is a heavy dependency (headless Chromium, hundreds of megabytes, noticeably slower to start than a plain Python script) — not a problem on the main PC, but worth remembering if the agent ever moves to weaker hardware (see "Moving the agent to a separate device" in [future-development.md](future-development.md)).

The issue's length is fixed: a cover plus up to 6 mosaic pages. If there's more news than fits, it's selected by importance, and long texts are shortened by the model. If fewer than 10 news items passed into print in a day, no issue is assembled (decided 2026-10-14, see "Closed questions" in [future-development.md](future-development.md)).

### Printing

The printer is an HP Color LaserJet Pro MFP M377dw, printing only over the local network (AirPrint or CUPS/IPP), format A4/Letter. Cloud printing is physically unavailable for this model: HP shut down the ePrint service in January 2023 (it had stopped shipping it on new printers back in 2020), and Google Cloud Print was shut down by Google even earlier, at the end of 2020. This means the printing process must sit on the same local network as the printer — a cloud-based architecture for this printer isn't under consideration. Behavior when the printer is unreachable (off, out of paper, no connection) is an open question.

### Launch

**Revisited after the first implementation stage:** a schedule was originally planned (the PC waking via RTC alarm/Wake-on-LAN, auto-start, auto-shutdown when done) — this was dropped before implementing Stage 5 in favor of on-demand launch. The reason: the newspaper doesn't strictly need to be "exactly one copy, early every morning" — it needs to exist when the user decides to assemble it; the schedule and its supporting infrastructure (waking the PC, OS auto-start, RTC/WoL reliability) added complexity without necessity.

Instead: a local web console on the home PC with an "Assemble newspaper" button: the user opens it and triggers a run explicitly, whenever they want. The same run is also available from the terminal with a single command — for cases where a browser isn't wanted. There's still no separate long-running process, just as there never was: one script from button press to printing — the same run that used to be triggered by an alarm.

The collection window isn't tied to whether or when the previous run succeeded — it's always a fixed last 24 hours **from the moment the button is pressed**, and that was true even in the scheduled version (see "closed questions" in [future-development.md](future-development.md)). Dropping the schedule doesn't change this mechanic: a missed day isn't caught up on either way — it's just that "missed a day" used to mean "the PC didn't wake up", and now it means "the user didn't press the button".

## 3. What to build in the first version

- The channel list — hardcoded in config, no management UI
- A local console with an "Assemble newspaper" button (plus the same run from the terminal): on press — pulls each channel's last-24-hours history via Telethon → filters out junk with heuristics → hands ambiguous cases and deduplication to the LLM → renders a single image from a fixed page template → sends it to the printer over the local network
- Run logging — how many posts came in, how many were filtered out and why, what got printed. Without this, understanding the behavior of an agent that runs unattended during a run would be impossible

Importance and section-based personalization, a channel-management web UI, and a printed archive aren't part of the first version, but are part of the project's full, finished state — implemented in Stages 6-8 of [implementation-plan.md](implementation-plan.md) after the first version. Robust retry logic for an offline printer and tracking paper usage remain deferred with no assigned priority — in [future-development.md](future-development.md).

## 4. Recommended stack

**Python** — taken together: a mature MTProto client library (Telethon), Playwright for headless HTML-to-image rendering, plus LLM calls that drop easily into the same stack. There's no particular reason to pick a different language for this task — the whole pipeline is uniform (I/O plus on-demand batch processing), with no real performance load.

## 5. Open forks

Settled at the brief stage: where the agent physically lives (the main home PC), how channels are accessed (MTProto/Telethon under a separate helper account), the printer and how it prints (HP Color LaserJet Pro MFP M377dw, local network only), the rendering technology (HTML/CSS + Playwright). Revisited later (after Stages 0–1): how it's launched — not a schedule with PC wake-up, but a button in the local console, see "Launch" above.

Below is what's still deliberately unresolved, expanded on in [future-development.md](future-development.md):

- ~~State storage between runs~~ — closed: SQLite (see "Closed questions" in [future-development.md](future-development.md))
- ~~Rule for which duplicate version to keep~~ — closed 2026-10-14: the most detailed one, as judged by the LLM (same place)
- ~~Behavior when there isn't enough news for a period~~ — closed 2026-10-14: fewer than 10 news items and no issue is assembled (same place). **Open:** behavior when the printer is unreachable
- **Open:** channel-list format and how it's edited (Stage 8)
- ~~Collection-window boundaries when runs have fallen behind~~ — closed: always the last 24 hours from the button press, with no catch-up (same place)
