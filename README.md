# MovieBox Web

A small self-hosted web app that lets every device on your home network browse and watch from MovieBox in a normal browser.
Run it on one machine (a PC, a Raspberry Pi, or even an old Android phone with Termux) and open it from phones, tablets, laptops and smart TVs on the same Wi-Fi.

> **Unofficial.** Not affiliated with or endorsed by MovieBox. This project hosts and stores no media: it only fetches links from MovieBox's own servers at the moment you press play. Use it for personal purposes and follow the laws and terms that apply to you.

## Features

- Search, trending rows and detailed title pages (movies, series, anime)
- Seasons and episodes, plus alternate audio / dubbed versions
- Subtitles in many languages (SRT, VTT and ASS are converted for the browser)
- Plays in the browser: MP4, DASH and HLS, with a source / quality picker
- Continue watching, resume position, watched marks, My List
- Auto-play next episode, ±10s and skip-intro buttons, playback speed, fullscreen, keyboard shortcuts
- Installable to the home screen (PWA), arrow-key navigation for TVs and desktops
- Fallback sources: if MovieBox has nothing playable, or the video won't play, the app tries 4KHDHub, Dramachi and any Stremio addons you add, automatically. If no source has the title you can pick a provider and search by hand (⇄ Provider)
- Live TV from your own M3U playlists
- No database, no accounts (the optional ⚙ Sources box saves to `cache/config.json`)

## Requirements

- Python 3.8 or newer
- Internet access on the machine that runs the server

## Install

**Windows / macOS / Linux**

```bash
git clone https://github.com/devrohit2007/Moviebox-web moviebox-web
cd moviebox-web
python -m venv .venv
# Windows:  .venv\Scripts\activate        macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

**Android (Termux)**

```bash
pkg update -y && pkg install python git -y
git clone https://github.com/devrohit2007/Moviebox-web moviebox-web
cd moviebox-web
pip install -r requirements.txt
termux-wake-lock        # keeps the server alive while the screen is off
```

## Run

```bash
python server.py            # default port 8000
python server.py 9000       # custom port
```

The server prints two addresses:

```
Open on this phone:      http://127.0.0.1:8000
Open on other devices:   http://192.168.1.50:8000   (same Wi-Fi)
```

Open the second one in any browser on your network. On a phone, use the browser menu → **Add to Home screen** to install it like an app.

## Tips

- **Fixed address:** give the host machine a static IP (or a DHCP reservation in your router) so the link never changes.
- **Windows firewall:** if other devices can't connect, allow Python on *Private networks* when Windows asks.
- **Keep it running:** on a spare phone, keep it charging and disable battery optimization for Termux.
- **Your data:** My List, resume positions and settings are stored in each device's own browser. The server only keeps a session token in `cache/`.

## Keyboard shortcuts (player)

| Key | Action |
| --- | --- |
| Space / K | Play / pause |
| ← / → | Back / forward 10 s |
| ↑ / ↓ | Volume |
| S | Skip 85 s |
| N | Next episode |
| F | Fullscreen |
| M | Mute |
| Esc | Close player |

## Troubleshooting

| Problem | Fix |
| --- | --- |
| "All hosts exhausted" | No internet on the host, or the service is temporarily down. Try again later. |
| A title opens but won't play | The app switches source by itself. You can also use the source list or ⇄ Provider in the player. |
| 4KHDHub says it needs beautifulsoup4 | `pip install beautifulsoup4` |
| Page loads but no pictures | Check the host's internet connection. |
| Other devices can't open the link | Same Wi-Fi? Firewall? Correct IP printed by the server? |

## Project layout

```
server.py            web server + media proxy
moviebox.py          MovieBox API client and parsers
providers.py         fallback sources (4KHDHub, Dramachi, addons) and Live TV playlists
static/index.html    the whole front end
```

## License

MIT — see [LICENSE](LICENSE). Third-party notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
