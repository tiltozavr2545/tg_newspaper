## Installation

- **macOS** — download `TG-Newspaper-<version>-mac.dmg`, open it and drag **TG Newspaper** into "Applications".
- **Windows** — download `TG-Newspaper-<version>-windows-setup.exe` and run it. No admin rights needed.

First launch prepares the environment (Python, libraries, the browser used for layout) and takes **2–3 minutes**; requires internet. While it's preparing, a waiting page is shown in the browser, which then redirects on its own to the console at `http://localhost:8420/`, where the setup wizard walks you through every step.

## First launch (the app isn't code-signed)

- **macOS:** right-click the app → "Open" → "Open". Or try launching it normally, then go to "System Settings" → "Privacy & Security" → "Open Anyway".
- **Windows:** if SmartScreen shows "Windows protected your PC" — click "More info" → "Run anyway".

## Where your data lives

- macOS: `~/Library/Application Support/TG Newspaper`
- Windows: `%LOCALAPPDATA%\TG Newspaper`

That's where settings, the Telegram session, the issue database, logs, and the runtime environment live. Updating or removing the app doesn't touch this data; you can delete it by hand if you want to.

## Important

Gemini requires a proxy (Cloudflare Worker) when used from Russia — see the [README](https://github.com/tiltozavr2545/tg_newspaper#readme) for how to set it up. The "PT Serif"/"Superclarendon" layout fonts are built into macOS; on Windows, the page falls back to substitutes (Georgia/Times), so the look will differ.
