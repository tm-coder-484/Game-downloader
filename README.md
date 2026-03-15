# HTML5 Game Archiver

**Version 2.0**

A tool for archiving HTML5 games for fully offline play. It launches a headless Chromium browser pointed at a local caching proxy server, intercepts every network request the game makes, fetches and rewrites the assets, and saves them to disk — repeating until no new files are downloaded.

---

## Requirements

- Python 3.7+
- [Playwright](https://playwright.dev/python/)

Install dependencies:

```bash
pip install playwright
playwright install chromium
```

---

## Usage

```bash
# Interactive mode (prompts for URL)
python game_archiver.py

# Pass URL directly
python game_archiver.py https://example.com/mygame/

# Specify a custom port (default: 8080)
python game_archiver.py https://example.com/mygame/ --port 9000
```

---

## How It Works

1. **Probe** — The script fetches the provided URL, follows redirects, and identifies the HTML entry point and game origin.
2. **Cache server** — A local threaded HTTP server starts up. On a cache miss, it fetches the real file from the remote server, rewrites all URLs to point locally, saves the file to disk, and serves it.
3. **JS shim** — A small JavaScript shim is injected into the entry HTML to intercept `fetch()` and `XMLHttpRequest` calls that weren't caught at the source level, routing them through the local proxy. Ad/analytics hosts are stubbed out automatically.
4. **Crawl loop** — A headless Chromium browser opens the game through the local server. All network requests (scripts, images, audio, video, fonts, WebAssembly, etc.) are intercepted and routed through the cache. This repeats for up to 8 passes, stopping when a full pass downloads zero new files.
5. **Offline play** — Once the archive is complete, the game opens in your default browser via the local server. The server keeps running so any late-loaded assets are still cached automatically. Press `Ctrl+C` to stop.

---

## Configuration

These constants at the top of the file can be adjusted:

| Constant | Default | Description |
|---|---|---|
| `GAMES_DIR` | `"games"` | Directory where archived games are stored |
| `SERVER_HOST` | `"localhost"` | Host for the local server |
| `SERVER_PORT` | `8080` | Default port for the local server |
| `CRAWL_WAIT_S` | `4` | Seconds to wait after page load for JS to settle |
| `PASS_IDLE_S` | `3` | Seconds of network silence before a pass is considered done |
| `MAX_PASSES` | `8` | Maximum number of crawl passes before stopping |

---

## File Structure

Archived games are stored under the `games/` directory, organized by a slug derived from the game's hostname and path:

```
games/
└── example_com__mygame/
    ├── index.html
    ├── .archiver.json       # Stores original URL and origin
    ├── .ext_hosts.json      # Maps external hostnames to local slugs
    ├── ext/
    │   └── cdn_example_com/ # Assets from external origins
    └── ...                  # All other game assets
```

---

## Ad & Analytics Blocking

The following host keywords are automatically stubbed out (return empty `{}` responses) to prevent ads and analytics from interfering with archiving:

`google-analytics`, `googletagmanager`, `doubleclick`, `googlesyndication`, `adservice`, `facebook.net`, `twitter.com/widgets`, `hotjar`, `moatads`, `adnxs`, `adsystem`, `amazon-adsystem`, `gamemonetize.com`, `yyygames.com`

---

## Disclaimer

This tool is provided as-is for personal and educational use. Use it at your own risk. The author is not responsible for any consequences arising from its use, including but not limited to copyright infringement, violations of a website's terms of service, or any other legal or technical issues. It is your responsibility to ensure that your use of this tool complies with all applicable laws and the terms of service of any websites you interact with.

---

## Supported Asset Types

The archiver handles a wide range of file types including HTML, JavaScript, CSS, JSON, XML, images (PNG, JPG, GIF, WebP, SVG), audio (WAV, MP3, OGG), video (MP4, WebM), fonts (TTF, OTF, WOFF, WOFF2), and binary/game-specific formats.