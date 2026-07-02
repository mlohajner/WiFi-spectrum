# Architecture

`scan.py` is the entire application — a single-file fusion of Python, GTK, HTML, CSS, and JavaScript, compiled to a standalone binary with [Nuitka](https://nuitka.net/).

## The stack

WiFi Spectrum doesn't run on Electron, and it isn't trying to be a lightweight substitute for one. It's a different combination of the same idea — native chrome plus a web-rendered UI — built entirely on the GTK/WebKit stack instead of Chromium/Node/V8:

| Layer | Electron | WiFi Spectrum |
|---|---|---|
| Native shell | Node.js + Chromium (Blink/V8) | GTK 4 |
| Render engine | Chromium (bundled) | WebKitGTK (system-provided) |
| App logic | JavaScript | Python |
| UI | HTML/CSS/JS | HTML/CSS/JS |
| Backend calls | Node bindings / IPC | Direct Python calls (`nmcli`, `rfkill`) |

The two approaches solve the same problem — give a web UI a native window and access to the system — but land in very different places. Electron ships its own browser runtime with every app. WiFi Spectrum uses the WebKitGTK engine already present on the system, calls out to `nmcli`/`rfkill` directly as subprocesses from Python, and keeps the GTK layer to the bare minimum: window chrome and a couple of native dialogs (settings, connection prompts). Everything else — the spectrum graph, the network table, sorting, tooltips, theming — is plain HTML/CSS/JS rendered inside that WebKitGTK view.

The result is a genuine fusion rather than a JS-to-Python port of Electron's model: Python owns scanning, parsing, and state; GTK owns the OS-level window and native dialogs; WebKitGTK owns rendering and interaction. No IPC bridge, no bundled browser, no Node runtime — the "web app" and the "native app" share one process and one address space.

## Why WebKitGTK

WebKitGTK is the same engine behind GNOME Web (Epiphany), which means it's Wayland-native, not an X11-compatibility fallback. The spectrum graph, hover tooltips, and theming all render identically and correctly under Wayland — no XWayland involved.

## Why this split, not a "real" native UI

A hand-rolled GTK UI (Cairo drawing, custom widgets) could do the spectrum graph too, but the overlapping channel curves, live tooltips cross-linked between graph and table, and full theme responsiveness are the kind of interaction that HTML/CSS/JS expresses far more directly — and lets the whole visual layer be restyled without touching the Python backend at all.

## File layout

Currently a single file:

```
scan.py   — scanning logic (nmcli/rfkill), GTK window + WebKitGTK view setup,
            embedded HTML/CSS/JS template, theme detection
```

The HTML/CSS/JS is embedded as template strings inside `scan.py` and populated with live scan data (`NETWORKS` array, per-BSSID color assignments, etc.) at render time — there's no separate build step or bundler involved.

## Runtime dependencies

- `nmcli` (NetworkManager) — scanning and connecting
- `rfkill` — WiFi block-state detection
- GTK 4 + PyGObject
- WebKitGTK
- Python 3.14+

See the top-level [README](../README.md) for installation and usage.
