#!/usr/bin/env python3
"""
\\ scan \\   WiFi + Bluetooth Spectrum Analyser
             😃 by Mario Lohajner 2026
"""

import subprocess
import re
import argparse
import sys
import os
import tempfile
import json
import threading
import select
import warnings

try:
	import gi
	gi.require_version('Gtk', '4.0')
	gi.require_version('WebKit', '6.0')
	from gi.repository import Gtk, WebKit
	GTK_AVAILABLE = True
except Exception:
	GTK_AVAILABLE = False

from datetime import datetime
import time


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHANNEL_FREQ_24 = {
	1: 2412, 2: 2417, 3: 2422, 4: 2427, 5: 2432,
	6: 2437, 7: 2442, 8: 2447, 9: 2452, 10: 2457,
	11: 2462, 12: 2467, 13: 2472, 14: 2484,
}
FREQ_CHANNEL_24 = {v: k for k, v in CHANNEL_FREQ_24.items()}

CHANNEL_FREQ_5 = {}
for ch in range(36, 178, 4):
	CHANNEL_FREQ_5[ch] = 5000 + ch * 5
FREQ_CHANNEL_5 = {v: k for k, v in CHANNEL_FREQ_5.items()}

CHANNEL_FREQ_6 = {}
for ch in range(1, 234, 4):
	CHANNEL_FREQ_6[ch] = 5950 + ch * 5
FREQ_CHANNEL_6 = {v: k for k, v in CHANNEL_FREQ_6.items()}


def freq_to_channel(freq_mhz: int) -> int:
	if freq_mhz in FREQ_CHANNEL_24:
		return FREQ_CHANNEL_24[freq_mhz]
	if freq_mhz in FREQ_CHANNEL_5:
		return FREQ_CHANNEL_5[freq_mhz]
	if freq_mhz in FREQ_CHANNEL_6:
		return FREQ_CHANNEL_6[freq_mhz]
	if 2400 <= freq_mhz <= 2500:
		return round((freq_mhz - 2407) / 5)
	if 5000 <= freq_mhz <= 5900:
		return (freq_mhz - 5000) // 5
	if 5925 <= freq_mhz <= 7125:
		return (freq_mhz - 5950) // 5 + 1
	return 0

def detect_interface() -> str:
	"""Auto-detect first wireless interface."""
	try:
		out = subprocess.check_output(['iw', 'dev'], text=True)
		m = re.search(r'Interface\s+(\S+)', out)
		if m:
			return m.group(1)
	except Exception:
		pass
# fallback: check /sys/class/net
	for name in os.listdir('/sys/class/net'):
		if os.path.exists(f'/sys/class/net/{name}/wireless'):
			return name
	return 'wlan0'


def detect_adapter_info(interface: str) -> str:
	"""Return a descriptive lspci/lsusb line for the given interface, e.g.
	'2e:00.0 Network controller: MEDIATEK Corp. MT7925 ... [Filogic 360]'.
	Returns '' if it can't be detected (never asks for sudo)."""
	device_path = f'/sys/class/net/{interface}/device'
	try:
		real = os.path.realpath(device_path)
	except Exception:
		return ''

# PCI(e) device — the last part of the path looks like 0000:2e:00.0
# ----------------------------------------------------------------------
	m = re.search(r'([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F])$', real)
	if m:
		pci_addr = m.group(1)
		try:
			out = subprocess.check_output(['lspci', '-s', pci_addr], text=True).strip()
			if out:
				return out
		except Exception:
			pass

# USB device — walk up to the folder with idVendor/idProduct, then find it in lsusb
# ----------------------------------------------------------------------
	usb_path = real
	while usb_path and usb_path != '/':
		if os.path.exists(os.path.join(usb_path, 'idVendor')):
			try:
				with open(os.path.join(usb_path, 'idVendor')) as f:
					vendor = f.read().strip()
				with open(os.path.join(usb_path, 'idProduct')) as f:
					product = f.read().strip()
				out = subprocess.check_output(['lsusb'], text=True)
				for line in out.splitlines():
					if f'{vendor}:{product}' in line:
						return line.strip()
			except Exception:
				pass
			break
		usb_path = os.path.dirname(usb_path)

	return ''


def rfkill_status(interface: str = '', type_label: str = 'Wireless LAN') -> dict:
	"""Return {'idx': str|None, 'soft': bool, 'hard': bool} for the rfkill
	device matching `interface` (falls back to the first block whose radio
	type matches `type_label`). Used for both WiFi ('Wireless LAN') and
	Bluetooth ('Bluetooth') radios — same function, different label."""
	result = {'idx': None, 'soft': False, 'hard': False}
	try:
		out = subprocess.check_output(['rfkill', 'list'], text=True)
	except Exception:
		return result

	blocks = re.split(r'\n(?=\d+:)', out.strip())
	chosen = None
	if interface:
		for b in blocks:
			if interface in b:
				chosen = b
				break
	if chosen is None:
		for b in blocks:
			if re.search(rf':\s*{re.escape(type_label)}', b, re.I):
				chosen = b
				break
	if chosen is None:
		return result

	m = re.match(r'(\d+):', chosen)
	if m:
		result['idx'] = m.group(1)
	result['soft'] = bool(re.search(r'Soft blocked:\s*yes', chosen, re.I))
	result['hard'] = bool(re.search(r'Hard blocked:\s*yes', chosen, re.I))
	return result


def bt_rfkill_status() -> dict:
	"""Same rfkill check as WiFi (rfkill_status), but for the BT radio.
	No interface name to match against (BT devices don't expose one like
	wlan0), so this goes straight to the 'Bluetooth' type-label lookup."""
	return rfkill_status(interface='', type_label='Bluetooth')


def rfkill_unblock(idx: str) -> bool:
	try:
		r = subprocess.run(['rfkill', 'unblock', idx], capture_output=True, text=True, timeout=10)
		if r.returncode != 0:
			r = subprocess.run(['sudo', 'rfkill', 'unblock', idx], capture_output=True, text=True, timeout=10)
		return r.returncode == 0
	except Exception:
		return False


def link_is_up(interface: str) -> bool:
	try:
		out = subprocess.check_output(['ip', 'link', 'show', interface], text=True)
		first_line = out.split('\n')[0]
		return 'state UP' in out or ',UP' in first_line or ' UP ' in first_line
	except Exception:
		return True  # unknown -> don't nag


def link_set_up(interface: str) -> bool:
	try:
		r = subprocess.run(['ip', 'link', 'set', interface, 'up'], capture_output=True, text=True, timeout=10)
		if r.returncode != 0:
			r = subprocess.run(['sudo', 'ip', 'link', 'set', interface, 'up'], capture_output=True, text=True, timeout=10)
		return r.returncode == 0
	except Exception:
		return False


NMCLI_CONFIG_PATH = os.path.expanduser('~/.config/wifi-spectrum/nmcli_extra.conf')

NMCLI_DEFAULT_PARAMS = (
	"802-11-wireless.powersave 2\n"
	"ipv6.method disabled\n"
)


def load_nmcli_extra() -> str:
	try:
		with open(NMCLI_CONFIG_PATH, 'r') as f:
			return f.read()
	except FileNotFoundError:
		return NMCLI_DEFAULT_PARAMS
	except Exception:
		return NMCLI_DEFAULT_PARAMS


def save_nmcli_extra(text: str) -> bool:
	try:
		os.makedirs(os.path.dirname(NMCLI_CONFIG_PATH), exist_ok=True)
		with open(NMCLI_CONFIG_PATH, 'w') as f:
			f.write(text)
		return True
	except Exception as e:
		print(f'[!] Could not save nmcli config: {e}')
		return False


THEME_CONFIG_PATH = os.path.expanduser('~/.config/wifi-spectrum/theme.conf')

def load_theme_pref() -> str:
	"""Return 'auto' | 'dark' | 'light'. Defaults to 'auto'. (line 1 of theme.conf)"""
	try:
		with open(THEME_CONFIG_PATH, 'r') as f:
			first_line = f.readline().strip().lower()
			if first_line in ('auto', 'dark', 'light'):
				return first_line
	except Exception:
		pass
	return 'auto'


def load_bluetooth_pref() -> bool:
	"""Return True if Bluetooth scanning is enabled. Defaults to False (off).
	Read from line 2 of theme.conf: 'bluetooth=on' / 'bluetooth=off'."""
	try:
		with open(THEME_CONFIG_PATH, 'r') as f:
			lines = f.read().splitlines()
		if len(lines) >= 2:
			return lines[1].strip().lower() == 'bluetooth=on'
	except Exception:
		pass
	return False


def _save_prefs(theme_value: str, bluetooth_enabled: bool) -> bool:
	"""Write both theme.conf lines together (theme + bluetooth=on/off) so
	saving one preference never clobbers the other."""
	if theme_value not in ('auto', 'dark', 'light'):
		theme_value = 'auto'
	try:
		os.makedirs(os.path.dirname(THEME_CONFIG_PATH), exist_ok=True)
		with open(THEME_CONFIG_PATH, 'w') as f:
			f.write(f"{theme_value}\nbluetooth={'on' if bluetooth_enabled else 'off'}\n")
		return True
	except Exception as e:
		print(f'[!] Could not save theme/bluetooth config: {e}')
		return False


def save_theme_pref(value: str) -> bool:
	return _save_prefs(value, load_bluetooth_pref())


def save_bluetooth_pref(enabled: bool) -> bool:
	return _save_prefs(load_theme_pref(), enabled)


def detect_system_dark_mode() -> bool:
	"""Best-effort detection of the desktop's dark/light preference.

	WebKitGTK's own `prefers-color-scheme` support only reliably tracks
	GNOME (via xdg-desktop-portal / gtk-application-prefer-dark-theme).
	Cinnamon and several other DEs apply a dark GTK theme without ever
	flipping that property, so the CSS media query alone misses it.
	We actively probe a few different sources here instead."""
# Live GTK settings (covers GNOME, and any DE that does set this)
	if GTK_AVAILABLE:
		try:
			settings = Gtk.Settings.get_default()
			if settings is not None:
				if settings.get_property('gtk-application-prefer-dark-theme'):
					return True
				theme_name = settings.get_property('gtk-theme-name') or ''
				if 'dark' in theme_name.lower():
					return True
		except Exception:
			pass

# gsettings — covers Cinnamon and GNOME, which both mirror the
# active GTK theme name (and GNOME additionally exposes color-scheme)
	checks = [
		('org.gnome.desktop.interface', 'color-scheme'),
		('org.cinnamon.desktop.interface', 'gtk-theme'),
		('org.gnome.desktop.interface', 'gtk-theme'),
		('org.x.apps.portal', 'color-scheme'),
	]
	for schema, key in checks:
		try:
			out = subprocess.check_output(
				['gsettings', 'get', schema, key],
				text=True, stderr=subprocess.DEVNULL, timeout=3
			).strip().strip("'\"")
			if 'dark' in out.lower():
				return True
		except Exception:
			continue

	return False

THEME_STYLE_PATH = os.path.expanduser('~/.config/wifi-spectrum/theme.css')

THEME_DEFAULT_STYLE = """
/* WiFi Spectrum -- theme stylesheet.
 *
 * Edit anything here: colours, fonts, spacing, hover effects, or add
 * entirely new rules. Restart the app to see changes.
 *
 * Colour values below that start with an ampersand (like the ones in
 * the --accent lines just below) are auto-detected from your live
 * GTK theme and refreshed every time the app starts. Replace one with
 * a literal hex code (e.g. #ff00ff) to lock that colour in for good
 * -- it stops being auto-detected once you do.
 */
:root {
	--bg:		#FFF;
	--panel:	#EEE;
	--surface2:	#CCC;
	--table-bg:	#FFF;
	--border:	#999;
	--text:		#000;
	--dim:		#4a5a72;
	--accent:	&accent_light;
	--accent-contrast: &accent_contrast_light;
	--grid:		rgba(0,0,0,0.8);
}

@media (prefers-color-scheme: dark) {
	html:not([data-theme="light"]) {
		--bg:		#26241f;
		--panel:	#302e28;
		--surface2:	#3a382f;
		--table-bg:	#2a2823;
		--border:	#4a4638;
		--text:		#ece8dd;
		--dim:		#a8a396;
		--accent:	&accent_dark;
		--accent-contrast: &accent_contrast_dark;
	}
}

html[data-theme="dark"] {
	--bg:		#26241f;
	--panel:	#302e28;
	--surface2:	#3a382f;
	--table-bg:	#2a2823;
	--border:	#4a4638;
	--text:		#ece8dd;
	--dim:		#a8a396;
	--accent:	&accent_dark;
	--accent-contrast: &accent_contrast_dark;
}

html[data-theme="light"] {
	--bg:		#FFF;
	--panel:	#EEE;
	--surface2:	#CCC;
	--table-bg:	#FFF;
	--border:	#999;
	--text:		#000;
	--dim:		#4a5a72;
	--accent:	&accent_light;
	--accent-contrast: &accent_contrast_light;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

html { background: var(--bg); }

body {
	background: var(--bg);
	color: var(--text);
	font-family: sans-serif;
	min-height: 100vh;
	padding: 0 10px 10px 10px;
	margin:0;
	transition: background 0.15s, color 0.15s;
}

header {
	display: flex;
	flex-direction: column;
	margin-bottom: 10px;
	border-bottom: 1px solid var(--border);
	padding-bottom: 4px;
}
header h1 {
	font-family: monospace;
	font-size: 1.3rem;
	font-weight: 700;
	color: var(--accent);
	letter-spacing: 0.08em;
	border-bottom: 1px solid var(--border);
	text-transform: uppercase;
}
.header-sub {
	display: flex;
	align-items: baseline;
	justify-content: space-between;
	gap: 16px;
}
.header-sub span {
	font-size: 0.8rem;
	color: var(--dim);
	font-family: monospace;
}

.iface-info {
	display: flex;
	flex-direction: column;
	line-height: 1.3;
	font-family: monospace;
}
.iface-desc {
	font-size: 0.8rem;
	color: var(--dim);
}
.iface-name {
	font-size: 0.8rem;
	color: var(--accent);
	font-weight:bold;
}

.tabs {
	display: flex;
	gap: 5px;
	margin-bottom: 0;
}
.tab {
	padding: 7px 22px;
	border: 1px solid var(--border);
	border-bottom:none;
	border-radius: 8px 8px 0 0;
	background: transparent;
	color: var(--dim);
	cursor: pointer;
	font-family: monospace;
	font-size: 1rem;
	letter-spacing: 0.05em;
	transition: all 0.15s;
}
.tab:hover { color: var(--text); border-color: var(--dim); }
.tab.active {
	background: var(--accent);
	border-color: var(--accent);
	color: var(--accent-contrast);
	font-weight: 700;
}

.band-panel { display: none; }
.band-panel.active { display: block; }

.chart-wrap {
	background: #222;
	border: 1px solid var(--border);
	border-radius: 0px 8px 4px 4px;
	padding: 16px 16px 8px;
	margin-bottom: 10px;
	overflow-x: auto;
}

svg.spectrum {
	display: block;
	overflow: visible;
}

.grid-line { stroke: rgba(255,255,255,0.3); stroke-width: 1; }
.axis-label {
	fill: #7a8fa8;
	font-family: monospace;
	font-size: 11px;
}
.ch-label {
	fill: #a0b4c8;
	font-family: monospace;
	font-size: 10px;
	text-anchor: middle;
}
.freq-label {
	fill: #5a7080;
	font-family: monospace;
	font-size: 9px;
	text-anchor: middle;
}

/* Network bell curves / BT bars */
.bell { opacity: 0.5; transition: opacity 0.15s; cursor: pointer; }
.bell:hover { opacity: 1; }
.bell.faded { opacity: 0.1; }
.bell-stroke { fill: none; stroke-width: 2; opacity: 0.9; }
.bar-stroke { fill: none; stroke-width: 2; opacity: 0.9; }

/* Tooltip — styled like .net-table: header bar + row separators */
#tooltip {
	position: fixed;
	background: var(--table-bg);
	border: 1px solid var(--border);
	border-radius: 6px;
	font-family: monospace;
	font-size: 0.75rem;
	pointer-events: none;
	opacity: 0;
	transition: opacity 0.1s;
	z-index: 100;
	max-width: 320px;
	color: var(--text);
	overflow: hidden; /* clip rows to the rounded corners, like the titlebar */
}
#tooltip.visible { opacity: 1; }
#tooltip .tt-ssid {
	padding: 5px 14px;
	font-family: monospace;
	font-size: 1rem;
	letter-spacing: 0.08em;
	color: var(--dim);
	border-bottom: 1px solid var(--border);
	text-transform: uppercase;
	background: var(--surface2);
}
#tooltip .tt-row {
	color: var(--dim);
	padding: 4px 14px;
	border-bottom: 1px solid var(--border);
}
#tooltip .tt-row span { color: var(--text); }
#tooltip .tt-uuid {
	color: var(--text);
	padding: 3px 14px 3px 26px;
	font-size: 0.68rem;
	white-space: nowrap;
	border-bottom: 1px solid var(--border);
}
#tooltip > *:last-child { border-bottom: none; }

/* Table */
.net-table {
	width: 100%;
	border-collapse: collapse;
	font-size: 0.9rem;
}
.net-table th {
	text-align: left;
	padding: 8px 12px;
	font-family: monospace;
	font-size: 0.8rem;
	letter-spacing: 0.07em;
	color: var(--dim);
	border-bottom: 1px solid var(--border);
	white-space: nowrap;
}
.net-table td {
	padding: 2px 12px;
	border-bottom: 1px solid var(--border);
	color: var(--text);
	white-space: nowrap;
	background: var(--table-bg);
}
.net-table tr:last-child td { border-bottom: none; }
.net-table tr { cursor: pointer; transition: background 0.1s; }
.net-table tr:hover td { background: rgba(0,212,255,0.1); }
.net-table tr.selected td { background: var(--table-bg); }

.dot {
	display: inline-block;
	width: 15px; height: 15px;
	border-radius: 50%;
	margin-right: 6px;
	vertical-align: middle;
	flex-shrink: 0;
}

.signal-bar {
	display: inline-block;
	height: 8px;
	border-radius: 2px;
	min-width: 4px;
	vertical-align: middle;
}

.table-wrap {
	background: var(--panel);
	border: 1px solid var(--border);
	border-radius: 8px 8px 4px 4px;
	overflow: hidden;
}
.table-title {
	padding: 5px 14px;
	font-family: monospace;
	font-size: 1rem;
	letter-spacing: 0.08em;
	color: var(--dim);
	border-bottom: 1px solid var(--border);
	text-transform: uppercase;
	background: var(--surface2);
}

.count-badge {
	display: inline-block;
	background: var(--border);
	color: #FFF;
	font-family: monospace;
	font-size: 0.9rem;
	padding: 1px 10px;
	border-radius: 10px;
	margin-left: 8px;
}

.sec-badge {
	display: inline-block;
	font-family: monospace;
	font-size: 0.8rem;
	padding: 3px 10px;
	border-radius: 4px;
	font-weight: 700;
	letter-spacing: 0.04em;
	color: var(--panel);
}
.sec-open  { background:#22c55e; }
.sec-wep   { background:#ef4444; }
.sec-wpa   { background:#f97316; }
.sec-wpa2  { background:#00d4ff; }
.sec-wpa3  { background:#a855f7; }

.btn-connect {
	padding: 3px 10px;
	font-family: monospace;
	font-size: 0.8rem;
	letter-spacing: 0.04em;
	background: #666;
	border: 1px solid var(--border);
	color: #FFF;
	border-radius: 4px;
	cursor: pointer;
	transition: all 0.15s;
}
.btn-connect:hover {
	background: var(--accent);
	color: var(--accent-contrast);
	font-weight: 700;
}
"""

def load_theme_style() -> str:
	try:
		with open(THEME_STYLE_PATH, 'r') as f:
			return f.read()
	except FileNotFoundError:
		try:
			os.makedirs(os.path.dirname(THEME_STYLE_PATH), exist_ok=True)
			with open(THEME_STYLE_PATH, 'w') as f:
				f.write(THEME_DEFAULT_STYLE)
		except Exception:
# If exposing the default style fails, return default style silently
			pass

		return THEME_DEFAULT_STYLE
	except Exception:
		return THEME_DEFAULT_STYLE

def resolve_theme(pref: str) -> str:
	"""Resolve a stored preference ('auto'|'dark'|'light') into a
	concrete 'dark' or 'light' value, actively detecting the system
	preference when set to 'auto'."""
	if pref in ('dark', 'light'):
		return pref
	return 'dark' if detect_system_dark_mode() else 'light'


# GNOME 47+ named accent presets (org.gnome.desktop.interface accent-color
# only ever returns one of these names, never a hex value)
ACCENT_COLOR_MAP = {
	'blue': '#3584e4', 'teal': '#2190a4', 'green': '#3a944a',
	'yellow': '#c88800', 'orange': '#ed5b00', 'red': '#e62d42',
	'pink': '#d56199', 'purple': '#9141ac', 'slate': '#6f8396',
}

DEFAULT_ACCENT = '#00d4ff'


def _accent_from_gsettings() -> str | None:
	try:
		out = subprocess.check_output(
			['gsettings', 'get', 'org.gnome.desktop.interface', 'accent-color'],
			text=True, stderr=subprocess.DEVNULL, timeout=3
		).strip().strip("'\"")
		return ACCENT_COLOR_MAP.get(out.lower())
	except Exception:
		return None


def detect_accent_color() -> str:
	"""Pull the *real* accent colour out of the current GTK theme instead
	of hard-coding one, so the UI actually matches the desktop."""
# Named CSS colours the theme itself defines — this is how
# libadwaita/GTK4 themes expose their accent, and is honoured by
# most themed GTK3-compat stylesheets too.
	if GTK_AVAILABLE:
		try:
			widget = Gtk.Label()
			# get_style_context()/lookup_color() are deprecated since GTK 4.10,
			# but GTK provides no replacement for querying named theme colours
			# (e.g. accent_bg_color) — Gtk.Widget.get_color() only exposes the
			# foreground colour. Silence the warning rather than chase a
			# "proper" fix that doesn't exist yet.
			with warnings.catch_warnings():
				warnings.simplefilter('ignore', DeprecationWarning)
				ctx = widget.get_style_context()
				for name in ('accent_bg_color', 'accent_color', 'theme_selected_bg_color'):
					ok, rgba = ctx.lookup_color(name)
					if ok:
						r = round(rgba.red * 255)
						g = round(rgba.green * 255)
						b = round(rgba.blue * 255)
						return '#{:02x}{:02x}{:02x}'.format(r, g, b)
		except Exception:
			pass

# GNOME 47+ accent-color setting (named preset -> hex)
	found = _accent_from_gsettings()
	if found:
		return found

# Fallback — the tool's original cyan
	return DEFAULT_ACCENT


def invert_hex_color(hex_color: str) -> str:
	"""Plain RGB inversion, used to derive the dark-mode accent from the
	real light-mode/theme accent colour."""
	hex_color = hex_color.lstrip('#')
	try:
		r = 255 - int(hex_color[0:2], 16)
		g = 255 - int(hex_color[2:4], 16)
		b = 255 - int(hex_color[4:6], 16)
		return '#{:02x}{:02x}{:02x}'.format(r, g, b)
	except Exception:
		return '#da7756'


def contrasting_text_color(hex_color: str) -> str:
	"""Pick black or white text so labels drawn on top of an accent
	colour stay legible no matter which hue was detected/inverted."""
	hex_color = hex_color.lstrip('#')
	try:
		r = int(hex_color[0:2], 16)
		g = int(hex_color[2:4], 16)
		b = int(hex_color[4:6], 16)
	except Exception:
		return '#000'
	luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
	return '#000' if luminance > 0.55 else '#fff'


def run_scan(interface: str) -> str:
	try:
		result = subprocess.run(
			['iw', 'dev', interface, 'scan'],
			capture_output=True, text=True, timeout=30
		)
		if result.returncode != 0:
			result = subprocess.run(
				['sudo', 'iw', 'dev', interface, 'scan'],
				capture_output=True, text=True, timeout=30
			)
	except subprocess.TimeoutExpired:
		pass
	return result.stdout

# ----------------------------------------------------------------------
# IW scan parser
# ----------------------------------------------------------------------
def parse_scan(raw: str) -> list[dict]:
	networks = []
	current = None

	def finalize():
		if current and current.get('bssid'):
			networks.append(current.copy())

	for line in raw.splitlines():
		line = line.strip()

# NEW BSS
# ----------------------------------------------------------------------
		m = re.match(r'^BSS\s+([\da-f:]+)', line, re.I)
		if m:
			finalize()

			current = {
				'bssid': m.group(1).upper(),
				'ssid': '',
				'freq': 0,
				'signal': -100.0,
				'channel': 0,
				'band': '',
				'width': None,
				'security': 'open',

				'_ht': {'enabled': False, 'secondary': 0},
				'_vht': {'enabled': False, 'width': None},
				'_he': {'enabled': False, 'width': None},
				'_eht': {'enabled': False, 'width': None},
				'_rsn': False,
				'_wpa': False,
				'_privacy': False,
				'_sae': False,
			}
			continue

		if not current:
			continue

# FREQ / BAND
# ----------------------------------------------------------------------
		m = re.match(r'^freq:\s*(\d+)', line)
		if m:
			freq = int(m.group(1))
			current['freq'] = freq
			current['channel'] = freq_to_channel(freq)

			if freq < 2500:
				current['band'] = '2.4GHz'
			elif freq < 5925:
				current['band'] = '5GHz'
			else:
				current['band'] = '6GHz'
			continue

# SIGNAL
# ----------------------------------------------------------------------
		m = re.match(r'^signal:\s*([-\d.]+)', line)
		if m:
			current['signal'] = float(m.group(1))
			continue

# SSID
# ----------------------------------------------------------------------
		m = re.match(r'^SSID:\s*(.*)', line)
		if m:
			current['ssid'] = m.group(1).strip() or '<hidden>'
			continue

# SECURITY
# ----------------------------------------------------------------------
		if re.match(r'^RSN:', line):
			current['_rsn'] = True
			continue
		if re.match(r'^WPA:', line):
			current['_wpa'] = True
			continue
		if re.match(r'^capability:.*Privacy', line, re.I):
			current['_privacy'] = True
			continue
		if re.search(r'\bSAE\b', line):
			current['_sae'] = True
			continue
		m = re.search(r'RSN.*Version', line, re.I)
		if m:
			current['_rsn'] = True
			continue

# HT (2.4 GHz only)
# ----------------------------------------------------------------------
		if 'HT operation' in line:
			current['_ht']['enabled'] = True
			continue

		m = re.match(r'.*secondary channel offset:\s*(\w+)', line, re.I)
		if m:
			offset = m.group(1).lower()
			current['_ht']['secondary'] = 0 if offset in ('no', 'none', '0') else 1
			continue

# VHT (ONLY 5 GHz)
# ----------------------------------------------------------------------
		if 'VHT operation' in line:
			if current['band'] == '5GHz':
				current['_vht']['enabled'] = True
			continue

		m = re.match(r'.*\* channel width:\s*(\d+)\s*\((\d+)\s*MHz\)', line, re.I)
		if m and current['band'] == '5GHz':
			current['_vht']['width'] = int(m.group(2))
			continue

		m = re.match(r'.*\* channel width:\s*(\d+)', line, re.I)
		if m and current['band'] == '5GHz':
			code = int(m.group(1))
			vht_map = {0: 20, 1: 80, 2: 160}
			current['_vht']['width'] = vht_map.get(code)
			continue

# HE (ONLY 5/6 GHz)
# ----------------------------------------------------------------------
		if 'HE Operation' in line:
			if current['freq'] >= 5000:
				current['_he']['enabled'] = True
			continue

		m = re.search(r'HE.*?(\d+)\s*MHz', line)
		if m and current['freq'] >= 5000:
			current['_he']['width'] = int(m.group(1))
			continue

# EHT (Wi-Fi 7, up to 320 MHz — 6 GHz primarily, also 5 GHz)
# ----------------------------------------------------------------------
		if 'EHT Operation' in line:
			if current['freq'] >= 5000:
				current['_eht']['enabled'] = True
			continue

		m = re.search(r'EHT.*?(\d+)\s*MHz', line)
		if m and current['freq'] >= 5000:
			current['_eht']['width'] = int(m.group(1))
			continue

	finalize()

	for n in networks:
		band = n['band']

# EHT (highest priority — Wi-Fi 7, up to 320 MHz)
# ----------------------------------------------------------------------
		if n['_eht']['width'] and n['band'] in ('5GHz', '6GHz'):
			n['width'] = n['_eht']['width']

# HE
# ----------------------------------------------------------------------
		elif n['_he']['width'] and n['band'] in ('5GHz', '6GHz'):
			n['width'] = n['_he']['width']

# VHT (5 GHz only)
# ----------------------------------------------------------------------
		elif band == '5GHz' and n['_vht']['width']:
			n['width'] = n['_vht']['width']

# HT (2.4 GHz only)
# ----------------------------------------------------------------------
		elif band == '2.4GHz':
			n['width'] = 40 if n['_ht']['secondary'] else 20

		else:
			n['width'] = 20

# HARD SAFETY GUARD (critical fix) — per-band sane upper bounds
# ----------------------------------------------------------------------
		if n['band'] == '2.4GHz' and n['width'] > 40:
			n['width'] = 40
		elif n['band'] == '5GHz' and n['width'] > 160:
			n['width'] = 160
		elif n['band'] == '6GHz' and n['width'] > 320:
			n['width'] = 320

# Determine security type
# ----------------------------------------------------------------------
		if n['_sae']:
			n['security'] = 'WPA3'
		elif n['_rsn']:
			n['security'] = 'WPA2'
		elif n['_wpa']:
			n['security'] = 'WPA'
		elif n['_privacy']:
			n['security'] = 'WEP'
		else:
			n['security'] = 'open'

		del n['_ht']
		del n['_vht']
		del n['_he']
		del n['_eht']
		del n['_rsn']
		del n['_wpa']
		del n['_privacy']
		del n['_sae']

	return networks


# ----------------------------------------------------------------------
# Bluetooth: continuous discovery via a single long-lived `bluetoothctl`
# session. BlueZ doesn't expose frequency-hop occupancy over the CLI, so
# this is a "devices + live attributes" view (Name, RSSI, Icon, UUIDs),
# not a true spectral occupancy view like the WiFi tabs.
# ----------------------------------------------------------------------

# bluetoothctl colourises its output with ANSI escapes EVEN when stdout is
# a pipe, not just a real terminal. If an escape sequence lands right
# between a label ("RSSI:", "Name:", ...) and its value, a plain regex
# silently fails to match and the field quietly stays on its placeholder —
# strip escapes before matching anything.
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _new_bt_device(mac: str) -> dict:
	return {'mac': mac, 'name': mac, 'rssi': -100, 'icon': '', 'uuids': []}


def _parse_bt_line(line: str, devices: dict, lock: threading.Lock) -> None:
	"""Update `devices` (mac -> record) in place based on one bluetoothctl
	output line. Handles the [NEW]/[CHG] Device events that actually feed
	the BT tab: RSSI, Name, Alias, Icon and UUIDs."""

	m = re.search(r'\[CHG\]\s+Device\s+([0-9A-F:]{17})\s+RSSI:\s+0x[0-9A-Fa-f]+\s+\((-?\d+)\)', line, re.I)
	if m:
		mac, rssi = m.group(1).upper(), int(m.group(2))
		with lock:
			d = devices.setdefault(mac, _new_bt_device(mac))
			d['rssi'] = rssi
		return

	m = re.search(r'\[NEW\]\s+Device\s+([0-9A-F:]{17})\s+(.+)', line, re.I)
	if m:
		mac, name = m.group(1).upper(), m.group(2).strip()
		with lock:
			d = devices.setdefault(mac, _new_bt_device(mac))
			if d['name'] == mac and name != mac:
				d['name'] = name
		return

	m = re.search(r'\[DEL\]\s+Device\s+([0-9A-F:]{17})', line, re.I)
	if m:
		mac = m.group(1).upper()
		with lock:
			devices.pop(mac, None)
		return

	m = re.search(r'\[CHG\]\s+Device\s+([0-9A-F:]{17})\s+Name:\s*(.+)', line, re.I)
	if m:
		mac, name = m.group(1).upper(), m.group(2).strip()
		with lock:
			d = devices.setdefault(mac, _new_bt_device(mac))
			d['name'] = name
		return

	m = re.search(r'\[CHG\]\s+Device\s+([0-9A-F:]{17})\s+Icon:\s*(.+)', line, re.I)
	if m:
		mac, icon = m.group(1).upper(), m.group(2).strip()
		with lock:
			d = devices.setdefault(mac, _new_bt_device(mac))
			d['icon'] = icon
		return

	m = re.search(r'\[CHG\]\s+Device\s+([0-9A-F:]{17})\s+UUIDs?:\s*(\S+)', line, re.I)
	if m:
		mac, uuid = m.group(1).upper(), m.group(2).strip()
		with lock:
			d = devices.setdefault(mac, _new_bt_device(mac))
			if uuid not in d['uuids']:
				d['uuids'].append(uuid)
		return


def _bt_reader_loop(devices: dict, lock: threading.Lock, stop_flag: threading.Event) -> None:
	"""Runs for the whole lifetime of the app in its own daemon thread:
	starts ONE `bluetoothctl` session, sends `scan on` ONCE, then just
	keeps reading and parsing whatever streams out of it. No fixed scan
	duration, no restart per cycle — this is what makes BT discovery
	continuous instead of a series of disconnected short bursts. The
	periodic UI refresh (same single `interval` as WiFi) just reads a
	snapshot of `devices` — it doesn't drive or gate the scan itself."""
	proc = None
	try:
		proc = subprocess.Popen(
			['bluetoothctl'],
			stdin=subprocess.PIPE, stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL, text=True, bufsize=1
		)
		proc.stdin.write('scan on\n')
		proc.stdin.flush()

		while not stop_flag.is_set():
			ready, _, _ = select.select([proc.stdout], [], [], 1.0)
			if not ready:
				continue  # just a poll tick so stop_flag gets re-checked
			raw_line = proc.stdout.readline()
			if not raw_line:
				break  # bluetoothctl exited / stdout closed
			line = _ANSI_RE.sub('', raw_line).strip()
			if line:
				_parse_bt_line(line, devices, lock)
	except Exception as e:
		print(f'[!] BT reader error: {e}')
	finally:
		if proc is not None:
			try:
				proc.stdin.write('scan off\n')
				proc.stdin.write('exit\n')
				proc.stdin.flush()
			except Exception:
				pass
			try:
				proc.terminate()
			except Exception:
				pass

# Colour deterministic per BSSID/MAC
def bssid_color(bssid: str) -> str:
	return '#' + bssid[-8:].replace(':', '')

# ----------------------------------------------------------------------
# HTML/SVG generator
# ----------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"{theme_attr}>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WiFi Spectrum – {timestamp}</title>
<style>
{style}
</style>
</head>
<body>

<header>
  <h1 style="font-size:2rem">&#x25A0;&nbsp;WiFi&nbsp;SPECTRUM</h1>
  <div class="header-sub">
	<span class="iface-info">
		<span class="iface-desc">{adapter_desc}</span>
		<span class="iface-name">{interface}</span>
	</span>
	<span id="scan-status" style="font-size:0.8rem;color:var(--dim)">● Scanning…</span>
  </div>
</header>

<div class="tabs">
  <button class="tab active" onclick="showBand('24')">2.4 GHz <span class="count-badge" id="cnt24"></span></button>
  <button class="tab" onclick="showBand('5')">5 GHz <span class="count-badge" id="cnt5"></span></button>
  <button class="tab" onclick="showBand('6')">6 GHz <span class="count-badge" id="cnt6"></span></button>
  <button class="tab" id="tab-bt" onclick="showBand('bt')"{bt_tab_hidden}>Bluetooth <span class="count-badge" id="cntbt"></span></button>
</div>

<div id="band24" class="band-panel active"></div>
<div id="band5"  class="band-panel"></div>
<div id="band6"  class="band-panel"></div>
<div id="bandbt" class="band-panel"></div>

<div id="tooltip"></div>

<script>
const NETWORKS = {networks_json};
const BT_DEVICES = {bt_json};

function bssidColor(bssid) {{
  return '#' + bssid.slice(-8).replace(/:/g, '');
}}

// ---- Theme control (called from Python via evaluate_javascript) ----
function setTheme(mode) {{
  if (mode === 'dark') {{
	document.documentElement.setAttribute('data-theme', 'dark');
  }} else if (mode === 'light') {{
	document.documentElement.setAttribute('data-theme', 'light');
  }} else {{
	document.documentElement.removeAttribute('data-theme');
  }}
}}

// ---- Bluetooth on/off control (called from Python via evaluate_javascript
// when the "Bluetooth scan" switch is toggled in Settings) ----
function setBluetoothEnabled(enabled) {{
  const tab = document.getElementById('tab-bt');
  if (!tab) return;
  if (enabled) {{
    tab.style.display = '';
  }} else {{
    tab.style.display = 'none';
    if (tab.classList.contains('active')) {{
      tab.classList.remove('active');
      document.getElementById('bandbt').classList.remove('active');
      const first = document.querySelector('.tab');
      if (first) first.classList.add('active');
      document.getElementById('band24').classList.add('active');
    }}
  }}
}}

// ---- Live update entry point (called from Python via evaluate_javascript) ----
function updateNetworks(newNetworks) {{
  NETWORKS.length = 0;
  newNetworks.forEach(n => {{
    n.color = bssidColor(n.bssid);
    NETWORKS.push(n);
  }});

  const now = new Date().toLocaleTimeString();
  const status = document.getElementById('scan-status');
  status.innerHTML = '● Updated ' + now + '&nbsp;<br><b>' + NETWORKS.length + ' networks</b>';
  status.style.color = 'var(--accent)';
  setTimeout(() => status.style.color = 'var(--dim)', 1200);

  document.getElementById('cnt24').textContent = NETWORKS.filter(n => n.band === '2.4GHz').length;
  document.getElementById('cnt5').textContent  = NETWORKS.filter(n => n.band === '5GHz').length;
  document.getElementById('cnt6').textContent  = NETWORKS.filter(n => n.band === '6GHz').length;
  buildBand(NETWORKS.filter(n => n.band === '2.4GHz'), 'band24', '2.4GHz');
  buildBand(NETWORKS.filter(n => n.band === '5GHz'),  'band5',  '5GHz');
  buildBand(NETWORKS.filter(n => n.band === '6GHz'),  'band6',  '6GHz');
}}

// ---- Live update entry point for Bluetooth (called from Python every
// single `interval` tick, same cadence as WiFi — the continuous scan
// itself runs independently in the background, this just pushes a
// snapshot of whatever's known so far) ----
function updateBTDevices(newDevices) {{
  BT_DEVICES.length = 0;
  newDevices.forEach(d => {{
    d.color = bssidColor(d.mac);
    BT_DEVICES.push(d);
  }});

  document.getElementById('cntbt').textContent = BT_DEVICES.length;
  buildBT(BT_DEVICES, 'bandbt');
}}

// ---- Tab switching ----
function showBand(b) {{
  document.querySelectorAll('.band-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('band' + b).classList.add('active');
  event.target.closest('.tab').classList.add('active');
}}

// ---- Tooltip ----
const tip = document.getElementById('tooltip');
function showTip(e, net) {{
  const sig = net.signal;
  const quality = sig >= -50 ? 'Excellent' : sig >= -60 ? 'Good' : sig >= -70 ? 'Fair' : 'Weak';
  tip.innerHTML = `
	<div class="tt-ssid">${{net.ssid}}</div>
	<div class="tt-row">BSSID &nbsp;<span>${{net.bssid}}</span></div>
	<div class="tt-row">Channel <span>${{net.channel}} (${{net.freq}} MHz)</span></div>
	<div class="tt-row">Width &nbsp;&nbsp;<span>${{net.width}} MHz</span></div>
	<div class="tt-row">Signal &nbsp;<span>${{sig}} dBm – ${{quality}}</span></div>
	<div class="tt-row">Band &nbsp;&nbsp;&nbsp;<span>${{net.band}}</span></div>
  `;
  tip.classList.add('visible');
  moveTip(e);
}}
function showBTTip(e, dev) {{
  const sig = dev.rssi;
  const quality = sig >= -50 ? 'Excellent' : sig >= -60 ? 'Good' : sig >= -70 ? 'Fair' : 'Weak';
  const uuids = dev.uuids || [];
  const uuidRows = uuids.length
	? uuids.map(u => `<div class="tt-uuid">${{u}}</div>`).join('')
	: '';
  tip.innerHTML = `
	<div class="tt-ssid">${{dev.name}}</div>
	<div class="tt-row">MAC &nbsp;&nbsp;&nbsp;<span>${{dev.mac}}</span></div>
	<div class="tt-row">Signal &nbsp;<span>${{sig}} dBm – ${{quality}}</span></div>
	<div class="tt-row">Type &nbsp;&nbsp;&nbsp;<span>${{dev.icon}}</span></div>
	<div class="tt-row">UUIDs</div>
	${{uuidRows}}
  `;
  tip.classList.add('visible');
  moveTip(e);
}}
function moveTip(e) {{
  const x = e.clientX + 14, y = e.clientY - 10;
  tip.style.left = Math.min(x, window.innerWidth - 340) + 'px';
  tip.style.top = Math.max(y, 10) + 'px';
}}
function hideTip() {{ tip.classList.remove('visible'); }}

// ---- Signal → bar width ----
function sigWidth(s) {{ return Math.max(4, Math.round((s + 100) * 1.8)); }}
function sigColor(s) {{
  if (s >= -50) return '#22c55e';
  if (s >= -65) return '#ffd700';
  if (s >= -75) return '#f97316';
  return '#ef4444';
}}

// ---- Build spectrum chart + table for one band ----
const tableSortState = {{}};   // containerId -> {{key, dir}}
const lastNetsByContainer = {{}}; // containerId -> raw nets array (for re-sort without rebuilding chart)

function buildBand(nets, containerId, band) {{
  const container = document.getElementById(containerId);
  if (!nets.length) {{
	container.innerHTML = '<p style="padding:24px;color:var(--dim);font-family:monospace;font-size:2rem;">No networks found on ' + band + '</p>';
	return;
  }}

  const is24 = band === '2.4GHz';
  const is6  = band === '6GHz';

  // Frequency range
  const freqs = nets.map(n => n.freq);
  const widths = nets.map(n => n.width);
  const minFreq = Math.min(...freqs.map((f,i) => f - widths[i])) - 20;
  const maxFreq = Math.max(...freqs.map((f,i) => f + widths[i])) + 20;
  const freqRange = maxFreq - minFreq;

  // SVG dimensions
  const W = Math.max(900, freqRange * 1.8);
  const H = 300;
  const PAD_L = 20, PAD_R = 20, PAD_T = 20, PAD_B = 20;
  const cW = W - PAD_L - PAD_R;
  const cH = H - PAD_T - PAD_B;

  const fx = f => PAD_L + ((f - minFreq) / freqRange) * cW;
  // signal -30 (top) to -100 (bottom)
  const sy = s => PAD_T + ((s - (-30)) / ((-100) - (-30))) * cH;

  let svgParts = [];
  svgParts.push(`<svg class="spectrum" width="${{W}}" height="${{H}}" viewBox="0 0 ${{W}} ${{H}}" xmlns="http://www.w3.org/2000/svg">`);

  // Background grid
  for (let db = -30; db >= -100; db -= 10) {{
	const y = sy(db);
	svgParts.push(`<line class="grid-line" x1="${{PAD_L}}" y1="${{y}}" x2="${{W - PAD_R}}" y2="${{y}}"/>`);
	svgParts.push(`<text class="axis-label" x="${{PAD_L - 6}}" y="${{y + 4}}" text-anchor="end">${{db}}</text>`);
  }}

  // Channel markers
  const chans = is24
	? Object.entries(CHANNEL_FREQ_24_JS).map(([c, f]) => [parseInt(c), f])
	: is6
	? Object.entries(CHANNEL_FREQ_6_JS).map(([c, f]) => [parseInt(c), f])
	: Object.entries(CHANNEL_FREQ_5_JS).map(([c, f]) => [parseInt(c), f]);

  chans.forEach(([ch, f]) => {{
	if (f < minFreq - 5 || f > maxFreq + 5) return;
	const x = fx(f);
	svgParts.push(`<line class="grid-line" x1="${{x}}" y1="${{PAD_T}}" x2="${{x}}" y2="${{H - PAD_B}}" stroke-dasharray="3,4"/>`);
	svgParts.push(`<text class="ch-label" x="${{x}}" y="${{H - PAD_B + 14}}">${{ch}}</text>`);
	svgParts.push(`<text class="freq-label" x="${{x}}" y="${{H - PAD_B + 26}}">${{f}}</text>`);
  }});

  // Bottom axis line
  svgParts.push(`<line stroke="var(--border)" stroke-width="1" x1="${{PAD_L}}" y1="${{H - PAD_B}}" x2="${{W - PAD_R}}" y2="${{H - PAD_B}}"/>`);

  // Bell curves – strongest signal first (background), weakest last (foreground)
  const sorted = [...nets].sort((a, b) => b.signal - a.signal);
  sorted.forEach(net => {{
	const cx = fx(net.freq);
	const halfW = (net.width / 2) / freqRange * cW;
	const peak = sy(net.signal);
	const base = sy(-100);
	const pts = 60;
	let fillPts = [];
	let strokePts = [];
	for (let i = 0; i <= pts; i++) {{
	  const t = (i / pts) * 2 - 1; // -1 to 1
	  const x = cx + t * halfW * 1.4;
	  const y = base - (base - peak) * Math.exp(-(t * t) / 0.28);
	  fillPts.push(`${{i === 0 ? 'M' : 'L'}}${{x.toFixed(1)}},${{y.toFixed(1)}}`);
	  strokePts.push(`${{i === 0 ? 'M' : 'L'}}${{x.toFixed(1)}},${{y.toFixed(1)}}`);
	}}
	fillPts.push(`L${{(cx + halfW * 1.4).toFixed(1)}},${{base}} L${{(cx - halfW * 1.4).toFixed(1)}},${{base}} Z`);

	const id = 'bell_' + net.bssid.replace(/:/g,'');
	svgParts.push(`
	  <path id="${{id}}_fill" class="bell" data-bssid="${{net.bssid}}"
		d="${{fillPts.join(' ')}}" fill="${{net.color}}"
		onmouseenter="onBellHover(event,'${{net.bssid}}')"
		onmouseleave="onBellLeave(event,'${{net.bssid}}')"
		onmousemove="moveTip(event)"
	  />
	  <path id="${{id}}_stroke" class="bell-stroke" data-bssid="${{net.bssid}}"
		d="${{strokePts.join(' ')}}" stroke="${{net.color}}"
		pointer-events="none"
	  />
	`);
  }});

  svgParts.push('</svg>');

  lastNetsByContainer[containerId] = nets;

  container.innerHTML = `
	<div class="chart-wrap">${{svgParts.join('')}}</div>
	<div id="${{containerId}}-tablewrap">${{renderNetTable(nets, containerId)}}</div>
  `;

  attachTableEvents(containerId);
}}

// ---- Sortable table rendering (table only — chart above is untouched) ----
const SORT_KEYS = {{
  ssid:     n => n.ssid.toLowerCase(),
  bssid:    n => n.bssid,
  channel:  n => n.channel,
  width:    n => n.width,
  signal:   n => n.signal,
  freq:     n => n.freq,
  security: n => n.security.toLowerCase(),
}};

function renderNetTable(nets, containerId) {{
  const state = tableSortState[containerId] || {{ key: 'bssid', dir: 1 }};
  const getKey = SORT_KEYS[state.key] || SORT_KEYS.bssid;
  const sortedNets = [...nets].sort((a, b) => {{
	const va = getKey(a), vb = getKey(b);
	if (va < vb) return -1 * state.dir;
	if (va > vb) return 1 * state.dir;
	return 0;
  }});

  const arrow = key => state.key === key ? (state.dir === 1 ? ' ▲' : ' ▼') : '';
  const th = (key, label) =>
	`<th onclick="sortTable('${{containerId}}','${{key}}')" style="cursor:pointer;user-select:none">${{label}}${{arrow(key)}}</th>`;

  const tableRows = sortedNets.map(net => {{
	const secClass = {{'open':'sec-open','WEP':'sec-wep','WPA':'sec-wpa','WPA2':'sec-wpa2','WPA3':'sec-wpa3'}}[net.security] || 'sec-wpa2';
	const needsPass = net.security !== 'open';
	return `
	<tr class="net-row" data-bssid="${{net.bssid}}">
	  <td><span class="dot" style="background:${{net.color}}"></span>${{net.ssid}}</td>
	  <td style="font-family:monospace">${{net.bssid}}</td>
	  <td style="font-family:monospace">${{net.channel}}</td>
	  <td style="font-family:monospace">${{net.width}}&thinsp;MHz</td>
	  <td>
		<span style="font-family:monospace">${{net.signal}}&thinsp;dBm</span>
		<span class="signal-bar" style="margin-left:6px;width:${{sigWidth(net.signal)}}px;background:${{sigColor(net.signal)}}"></span>
	  </td>
	  <td style="font-family:monospace">${{net.freq}}&thinsp;MHz</td>
	  <td><span class="sec-badge ${{secClass}}">${{net.security}}</span></td>
	  <td align="right"><button class="btn-connect" onclick="event.stopPropagation();event.stopImmediatePropagation();connectNet('${{net.bssid}}','${{net.ssid}}','${{net.security}}')">CONNECT</button></td>
	</tr>`;
  }}).join('');

  return `
	<div class="table-wrap">
	  <div class="table-title">Networks <span class="count-badge">${{nets.length}}</span></div>
	  <table class="net-table">
		<thead><tr>
		  ${{th('ssid','SSID')}}${{th('bssid','BSSID')}}${{th('channel','CH')}}${{th('width','WIDTH')}}${{th('signal','SIGNAL')}}${{th('freq','FREQ')}}${{th('security','SECURITY')}}<th></th>
		</tr></thead>
		<tbody>${{tableRows}}</tbody>
	  </table>
	</div>
  `;
}}

function sortTable(containerId, key) {{
  const state = tableSortState[containerId] || {{ key: 'bssid', dir: 1 }};
  if (state.key === key) {{
	state.dir *= -1;
  }} else {{
	state.key = key;
	state.dir = 1;
  }}
  tableSortState[containerId] = state;

  const nets = lastNetsByContainer[containerId] || [];
  document.getElementById(containerId + '-tablewrap').innerHTML = renderNetTable(nets, containerId);
  attachTableEvents(containerId);
}}

// ---- Bluetooth: color-coded signal bars (no frequency axis — BlueZ
// doesn't expose hop/channel occupancy, only device + live attributes) ----
const btSortState = {{ key: 'rssi', dir: -1 }};
let lastBTDevices = [];

function buildBT(devices, containerId) {{
  const container = document.getElementById(containerId);
  if (!devices.length) {{
	container.innerHTML = '<p style="padding:24px;color:var(--dim);font-family:monospace;font-size:2rem;">No BT devices found</p>';
	return;
  }}

  lastBTDevices = devices;

  const sorted = [...devices].sort((a, b) => b.rssi - a.rssi);

  const H = 300;
  const PAD_L = 40, PAD_R = 20, PAD_T = 20, PAD_B = 20;
  const cH = H - PAD_T - PAD_B;

  // Bars sit at an even spacing that shrinks as the device count grows
  // (within a fixed baseline graph width), but the spacing never drops
  // below MIN_STEP — past that point the chart itself grows wider
  // (triggering horizontal scroll) instead of squeezing bars together.
  const BASE_W = 900;
  const MIN_STEP = 40;
  const cW_base = BASE_W - PAD_L - PAD_R;
  let step = cW_base / sorted.length;
  let W;
  if (step < MIN_STEP) {{
	step = MIN_STEP;
	W = PAD_L + PAD_R + step * sorted.length;
  }} else {{
	W = BASE_W;
  }}
  const barW = Math.min(40, step * 0.6);

  const sy = s => PAD_T + ((s - (-30)) / ((-100) - (-30))) * cH; // -30..-100 dBm — grid unchanged

  // Bars run from -120 up to rssi instead of from -100 up to rssi, so a
  // device with no reading yet (default rssi -100) shows a small square.
  // This extra -100..-120 segment is a small fixed stub below the axis,
  // not stretched at the same rate as the main -30..-100 grid — otherwise
  // it'd be a disproportionately tall chunk (20dB at the grid's rate).
  const yAxis = H - PAD_B;          // unchanged position of the -100 grid line
  const FLOOR_H = 16;               // fixed stub height for the -100..-120 segment
  const yFloor = yAxis + FLOOR_H;   // where every bar's base sits
  const H_TOTAL = H + FLOOR_H;      // extra room in the SVG canvas for it

  let parts = [];
  parts.push(`<svg class="spectrum" width="${{W}}" height="${{H_TOTAL}}" viewBox="0 0 ${{W}} ${{H_TOTAL}}" xmlns="http://www.w3.org/2000/svg">`);

  for (let db = -30; db >= -100; db -= 10) {{
	const y = sy(db);
	parts.push(`<line class="grid-line" x1="${{PAD_L}}" y1="${{y}}" x2="${{W - PAD_R}}" y2="${{y}}"/>`);
	parts.push(`<text class="axis-label" x="${{PAD_L - 6}}" y="${{y + 4}}" text-anchor="end">${{db}}</text>`);
  }}
  parts.push(`<line stroke="var(--border)" stroke-width="1" x1="${{PAD_L}}" y1="${{yAxis}}" x2="${{W - PAD_R}}" y2="${{yAxis}}"/>`);

  sorted.forEach((dev, i) => {{
	const cx = PAD_L + step * i + step / 2;
	const y = sy(Math.max(dev.rssi, -100)); // clamp — grid scale stops at -100, stub covers below it
	const h = yFloor - y;
	const xL = (cx - barW / 2);
	const xR = (cx + barW / 2);
	const id = 'bar_' + dev.mac.replace(/:/g,'');
	// Outline covers left, top, right only — bottom stays open, sitting on
	// the floor strip (same convention as the bell-curve stroke).
	const outlinePts = `M${{xL.toFixed(1)}},${{yFloor.toFixed(1)}} L${{xL.toFixed(1)}},${{y.toFixed(1)}} L${{xR.toFixed(1)}},${{y.toFixed(1)}} L${{xR.toFixed(1)}},${{yFloor.toFixed(1)}}`;
	parts.push(`
	  <rect id="${{id}}_fill" class="bell" data-bssid="${{dev.mac}}"
		x="${{xL.toFixed(1)}}" y="${{y.toFixed(1)}}"
		width="${{barW.toFixed(1)}}" height="${{h.toFixed(1)}}"
		fill="${{dev.color}}" rx="2"
		onmouseenter="onBTBarHover(event,'${{dev.mac}}')"
		onmouseleave="onBTBarLeave(event,'${{dev.mac}}')"
		onmousemove="moveTip(event)"
	  />
	  <path id="${{id}}_stroke" class="bar-stroke" data-bssid="${{dev.mac}}"
		d="${{outlinePts}}" stroke="${{dev.color}}"
		pointer-events="none"
	  />
	`);
  }});

  parts.push('</svg>');

  container.innerHTML = `
	<div class="chart-wrap">${{parts.join('')}}</div>
	<div id="${{containerId}}-tablewrap">${{renderBTTable(devices)}}</div>
  `;

  attachBTTableEvents(containerId);
}}

const BT_SORT_KEYS = {{
  name: d => d.name.toLowerCase(),
  mac:  d => d.mac,
  rssi: d => d.rssi,
}};

function renderBTTable(devices) {{
  const getKey = BT_SORT_KEYS[btSortState.key] || BT_SORT_KEYS.rssi;
  const sorted = [...devices].sort((a, b) => {{
	const va = getKey(a), vb = getKey(b);
	if (va < vb) return -1 * btSortState.dir;
	if (va > vb) return 1 * btSortState.dir;
	return 0;
  }});

  const arrow = key => btSortState.key === key ? (btSortState.dir === 1 ? ' ▲' : ' ▼') : '';
  const th = (key, label) =>
	`<th onclick="sortBTTable('${{key}}')" style="cursor:pointer;user-select:none">${{label}}${{arrow(key)}}</th>`;

  const rows = sorted.map(dev => {{
	const uuidCount = (dev.uuids || []).length;
	return `
	<tr class="net-row" data-bssid="${{dev.mac}}">
	  <td><span class="dot" style="background:${{dev.color}}"></span>${{dev.name}}</td>
	  <td style="font-family:monospace">${{dev.mac}}</td>
	  <td>${{dev.icon}}</td>
	  <td>
		<span style="font-family:monospace">${{dev.rssi}}&thinsp;dBm</span>
		<span class="signal-bar" style="margin-left:6px;width:${{sigWidth(dev.rssi)}}px;background:${{sigColor(dev.rssi)}}"></span>
	  </td>
	  <td>${{uuidCount ? `<span class="count-badge">${{uuidCount}}</span>` : ''}}</td>
	</tr>`;
  }}).join('');

  return `
	<div class="table-wrap">
	  <div class="table-title">Devices <span class="count-badge">${{devices.length}}</span></div>
	  <table class="net-table">
		<thead><tr>${{th('name','DEVICE')}}${{th('mac','MAC')}}<th>TYPE</th>${{th('rssi','SIGNAL')}}<th>SERVICES</th></tr></thead>
		<tbody>${{rows}}</tbody>
	  </table>
	</div>
  `;
}}

function sortBTTable(key) {{
  if (btSortState.key === key) {{
	btSortState.dir *= -1;
  }} else {{
	btSortState.key = key;
	btSortState.dir = 1;
  }}
  document.getElementById('bandbt-tablewrap').innerHTML = renderBTTable(lastBTDevices);
  attachBTTableEvents('bandbt');
}}

function attachBTTableEvents(containerId) {{
  const container = document.getElementById(containerId);
  container.querySelectorAll('.net-row').forEach(row => {{
	const bssid = row.dataset.bssid;
	row.addEventListener('click', () => selectNet(bssid));
  }});
  if (selectedBssid) highlightNet(selectedBssid);
}}

function onBTBarHover(e, mac) {{
  const dev = BT_DEVICES.find(d => d.mac === mac);
  showBTTip(e, dev);
  highlightNet(mac);
}}
function onBTBarLeave(e, mac) {{
  hideTip();
  if (!selectedBssid) clearHighlight();
  else highlightNet(selectedBssid);
}}

function attachTableEvents(containerId) {{
  const container = document.getElementById(containerId);
  container.querySelectorAll('.net-row').forEach(row => {{
	const bssid = row.dataset.bssid;
	row.addEventListener('click', e => {{
	  if (e.target.closest('.btn-connect')) return;  // let button handle it
	  selectNet(bssid);
	}});
  }});
  // Re-apply current selection highlight after a re-render (sort or live update)
  if (selectedBssid) highlightNet(selectedBssid);
}}

// ---- Hover / select interaction ----
let selectedBssid = null;

function onBellHover(e, bssid) {{
  const net = NETWORKS.find(n => n.bssid === bssid);
  showTip(e, net);
  highlightNet(bssid);
}}
function onBellLeave(e, bssid) {{
  hideTip();
  if (!selectedBssid) clearHighlight();
  else highlightNet(selectedBssid);
}}

function selectNet(bssid) {{
  if (selectedBssid === bssid) {{
	selectedBssid = null;
	clearHighlight();
	document.querySelectorAll('.net-row').forEach(r => r.classList.remove('selected'));
  }} else {{
	selectedBssid = bssid;
	highlightNet(bssid);
	document.querySelectorAll('.net-row').forEach(r => {{
	  r.classList.toggle('selected', r.dataset.bssid === bssid);
	}});
  }}
}}

function highlightNet(bssid) {{
  document.querySelectorAll('.bell').forEach(el => {{
	el.classList.toggle('faded', el.dataset.bssid !== bssid);
  }});
  document.querySelectorAll('.net-row').forEach(r => {{
	r.style.opacity = r.dataset.bssid === bssid ? '1' : '0.4';
  }});
}}
function clearHighlight() {{
  document.querySelectorAll('.bell').forEach(el => el.classList.remove('faded'));
  document.querySelectorAll('.net-row').forEach(r => r.style.opacity = '1');
}}

// ---- Connect ----
function connectNet(bssid, ssid, security) {{
  try {{
    window.webkit.messageHandlers.connect.postMessage(
      JSON.stringify({{ bssid, ssid, security }})
    );
  }} catch(e) {{
  }}
}}

// ---- Channel frequency maps (JS side) ----
const CHANNEL_FREQ_24_JS = {ch24_json};
const CHANNEL_FREQ_5_JS  = {ch5_json};
const CHANNEL_FREQ_6_JS  = {ch6_json};

// ---- Init ----
const nets24 = NETWORKS.filter(n => n.band === '2.4GHz');
const nets5  = NETWORKS.filter(n => n.band === '5GHz');
const nets6  = NETWORKS.filter(n => n.band === '6GHz');
document.getElementById('cnt24').textContent = nets24.length;
document.getElementById('cnt5').textContent  = nets5.length;
document.getElementById('cnt6').textContent  = nets6.length;
document.getElementById('cntbt').textContent = BT_DEVICES.length;
buildBand(nets24, 'band24', '2.4GHz');
buildBand(nets5,  'band5',  '5GHz');
buildBand(nets6,  'band6',  '6GHz');
buildBT(BT_DEVICES, 'bandbt');
</script>
</body>
</html>
"""


def build_html(networks: list[dict], interface: str, adapter_desc: str = '', theme: str = 'auto', accent: str = None, bt_devices: list[dict] = None, bt_enabled: bool = False) -> str:
	bt_devices = bt_devices or []

# Assign colours
	for i, net in enumerate(networks):
		net['color'] = bssid_color(net['bssid'])
	for dev in bt_devices:
		dev['color'] = bssid_color(dev['mac'])

	timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

	if theme == 'dark':
		theme_attr = ' data-theme="dark"'
	elif theme == 'light':
		theme_attr = ' data-theme="light"'
	else:
		theme_attr = ''

# Real accent colour pulled from the GTK theme (not hard-coded); the
# dark-mode accent is a plain RGB invert of it, per user preference.
	accent_light = accent or detect_accent_color()
	accent_dark = invert_hex_color(accent_light)
	accent_contrast_light = contrasting_text_color(accent_light)
	accent_contrast_dark = contrasting_text_color(accent_dark)
	_style = load_theme_style()
	_style = (
		_style
		.replace("&accent_light", accent_light)
		.replace("&accent_dark", accent_dark)
		.replace("&accent_contrast_light", accent_contrast_light)
		.replace("&accent_contrast_dark", accent_contrast_dark)
	)
	return HTML_TEMPLATE.format(
		timestamp=timestamp,
		interface=interface,
		adapter_desc=adapter_desc or interface,
		total=len(networks),
		theme_attr=theme_attr,
		style=_style,
		bt_tab_hidden='' if bt_enabled else ' style="display:none"',
		networks_json=json.dumps(networks, ensure_ascii=False),
		bt_json=json.dumps(bt_devices, ensure_ascii=False),
		ch24_json=json.dumps({str(k): v for k, v in CHANNEL_FREQ_24.items()}),
		ch5_json=json.dumps({str(k): v for k, v in CHANNEL_FREQ_5.items()}),
		ch6_json=json.dumps({str(k): v for k, v in CHANNEL_FREQ_6.items()}),
	)



def show_gtk(html: str, interface: str, interval: int = 5):
	if not GTK_AVAILABLE:
		print('[!] GTK/WebKit not available, HTML generated only.')
		return

	from gi.repository import GLib

	app = Gtk.Application()
	_web_ref = []        # mutable container so the thread can reach the WebView
	_banner_ref = []      # mutable container: [banner_box, banner_label, banner_button] (WiFi)
	_bt_banner_ref = []   # same shape, stacked below, for Bluetooth
	_stop_flag = threading.Event()
	_nmcli_extra = {'text': load_nmcli_extra()}  # custom nmcli property=value lines, persisted to disk
	_theme_pref = {'value': load_theme_pref()}   # 'auto' | 'dark' | 'light', persisted to disk
	_bt_enabled = {'value': load_bluetooth_pref()}  # bool, persisted to disk (default off)

# Continuous BT discovery state — one persistent bluetoothctl session
# (started/stopped on demand, see _start_bt_reader/_stop_bt_reader),
# fed by _bt_reader_loop(). The main _scan_loop() below just reads a
# snapshot of this dict every `interval` tick; it never starts/stops
# the scan itself. `interval` is the SINGLE source of truth for both
# WiFi scan cadence and the BT UI refresh cadence — no separate BT
# interval anywhere. When Bluetooth scanning is disabled in Settings,
# no rfkill check runs, no bluetoothctl session is started, and no BT
# data is pushed to the WebView at all.
	_bt_devices = {}
	_bt_lock = threading.Lock()
	_bt_reader = {'thread': None, 'stop': threading.Event()}

	def _start_bt_reader():
		"""(Re)start the persistent bluetoothctl session. No-op if one is
		already running."""
		if _bt_reader['thread'] is not None and _bt_reader['thread'].is_alive():
			return
		_bt_reader['stop'] = threading.Event()
		with _bt_lock:
			_bt_devices.clear()
		t = threading.Thread(
			target=_bt_reader_loop, args=(_bt_devices, _bt_lock, _bt_reader['stop']), daemon=True
		)
		_bt_reader['thread'] = t
		t.start()

	def _stop_bt_reader():
		"""Stop the bluetoothctl session (if any) and forget any devices
		collected so far."""
		_bt_reader['stop'].set()
		_bt_reader['thread'] = None
		with _bt_lock:
			_bt_devices.clear()

# Cumulative WiFi state — keyed by BSSID
	_wifi_networks = {}

# ----------------------------------------------------------------------
# Background scan loop – runs in a daemon thread. Drives BOTH the
# WiFi scan AND the periodic Bluetooth UI refresh, on one interval.
# ----------------------------------------------------------------------
	def _scan_loop():
		first = True
		while not _stop_flag.is_set():
			if not first:
# single interval for WiFi scan + BT UI refresh
				_stop_flag.wait(interval)
			if _stop_flag.is_set():
				break
			if first:
# give WebKit time to load the HTML
				time.sleep(1)
			first = False

# ----------------------------------------------------------------------
# WiFi — passive pre-flight checks: never act automatically,
# just surface state via the GTK banner and skip the scan.
# ----------------------------------------------------------------------
			rf = rfkill_status(interface)
			if rf['hard']:
				GLib.idle_add(_show_banner,
					f'{interface}: hardware kill-switch active (hard blocked) - flip the hardware switch!',
					None)
			elif rf['soft']:
				GLib.idle_add(_show_banner,
					f'{interface} is rfkill-blocked (WiFi is disabled).',
					('UNBLOCK', lambda: rfkill_unblock(rf['idx'])))
			elif not link_is_up(interface):
				GLib.idle_add(_show_banner,
					f'{interface} is administratively down.',
					('START', lambda: link_set_up(interface)))
			else:
				GLib.idle_add(_hide_banner)
				print(f'[*] Background scan on {interface}…')
				try:
					raw = run_scan(interface)
					if raw.strip():
						networks = parse_scan(raw)
						for n in networks:
							n['color'] = bssid_color(n['bssid'])
							_wifi_networks[n['bssid']] = n
						wifi_snapshot = list(_wifi_networks.values())
						js = f'updateNetworks({json.dumps(wifi_snapshot, ensure_ascii=False)})'
						if _web_ref:
							GLib.idle_add(_push_update, _web_ref[0], js)
					else:
						print('[!] Empty scan result, skipping.')
				except Exception as e:
					print(f'[!] Scan error: {e}')

# ----------------------------------------------------------------------
# Bluetooth — same passive pre-flight pattern. The scan
# itself is already running continuously in the background
# (_bt_reader_loop); this just pushes a snapshot of what's
# been collected so far into the UI. Skipped entirely when
# Bluetooth scanning is disabled: no rfkill check, no
# bluetoothctl, no data pushed to the WebView.
# ----------------------------------------------------------------------
			if not _bt_enabled['value']:
				GLib.idle_add(_hide_bt_banner)
				continue

			btrf = bt_rfkill_status()
			if btrf['hard']:
				GLib.idle_add(_show_bt_banner,
					'Bluetooth: hardware kill-switch active (hard blocked) - flip the hardware switch!',
					None)
			elif btrf['soft']:
				GLib.idle_add(_show_bt_banner,
					'Bluetooth is rfkill-blocked (BT is disabled).',
					('UNBLOCK', lambda: rfkill_unblock(btrf['idx'])))
			else:
				GLib.idle_add(_hide_bt_banner)
				with _bt_lock:
					bt_snapshot = [dict(d) for d in _bt_devices.values()]
				for d in bt_snapshot:
					d['color'] = bssid_color(d['mac'])
				js = f'updateBTDevices({json.dumps(bt_snapshot, ensure_ascii=False)})'
				if _web_ref:
					GLib.idle_add(_push_update, _web_ref[0], js)

	def _show_banner(message: str, action):
		"""action is None (info-only) or (label, callback) tuple. Runs on
		the GTK main loop. Never executes anything by itself — only on
		explicit button click."""
		if not _banner_ref:
			return False
		box, label, button = _banner_ref
		label.set_text(message)
		label.set_halign(Gtk.Align.END)
		label.set_hexpand(True)
		label.set_xalign(1.0)
		if action:
			btn_label, callback = action
			button.set_label(btn_label)
			button.set_visible(True)

			def _on_click(_btn):
				button.set_sensitive(False)
				def _worker():
					callback()
					GLib.idle_add(_hide_banner)
				threading.Thread(target=_worker, daemon=True).start()

# disconnect any previous handler before reconnecting
			if getattr(button, '_handler_id', None):
				try:
					button.disconnect(button._handler_id)
				except Exception:
					pass
			button._handler_id = button.connect('clicked', _on_click)
			button.set_sensitive(True)
		else:
			button.set_visible(False)
		box.set_margin_end(50)
		box.set_visible(True)
		return False

	def _hide_banner():
		if _banner_ref:
			_banner_ref[0].set_visible(False)
		return False

	def _show_bt_banner(message: str, action):
		"""Same as _show_banner, but for the stacked Bluetooth banner row."""
		if not _bt_banner_ref:
			return False
		box, label, button = _bt_banner_ref
		label.set_text(message)
		label.set_halign(Gtk.Align.END)
		label.set_hexpand(True)
		label.set_xalign(1.0)
		if action:
			btn_label, callback = action
			button.set_label(btn_label)
			button.set_visible(True)

			def _on_click(_btn):
				button.set_sensitive(False)
				def _worker():
					callback()
					GLib.idle_add(_hide_bt_banner)
				threading.Thread(target=_worker, daemon=True).start()

			if getattr(button, '_handler_id', None):
				try:
					button.disconnect(button._handler_id)
				except Exception:
					pass
			button._handler_id = button.connect('clicked', _on_click)
			button.set_sensitive(True)
		else:
			button.set_visible(False)
		box.set_margin_end(50)
		box.set_visible(True)
		return False

	def _hide_bt_banner():
		if _bt_banner_ref:
			_bt_banner_ref[0].set_visible(False)
		return False

	def _push_update(web, js: str):
		"""Called on the GTK main loop (safe to call WebKit from here)."""
		web.evaluate_javascript(js, -1, None, None, None, None, None)
		return False

	def _apply_theme(value: str):
		"""Push the theme choice into the live WebView (main loop only)."""
		if _web_ref:
			web = _web_ref[0]
			web.evaluate_javascript(f"setTheme('{value}')", -1, None, None, None, None, None)
		return False

	def _apply_bt_enabled(enabled: bool):
		"""Push the Bluetooth tab visibility into the live WebView (main
		loop only)."""
		if _web_ref:
			web = _web_ref[0]
			js_bool = 'true' if enabled else 'false'
			web.evaluate_javascript(f"setBluetoothEnabled({js_bool})", -1, None, None, None, None, None)
		return False

	def _show_connect_dialog(win, ssid: str, bssid: str, security: str):
		dialog = Gtk.Dialog(title="Connect to WiFi", transient_for=win, modal=True)
		dialog.set_default_size(380, 0)

		box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
		box.set_margin_top(16)
		box.set_margin_bottom(10)
		box.set_margin_start(20)
		box.set_margin_end(0)

		sec_label = Gtk.Label(label=f"Security: {security}")
		sec_label.set_halign(Gtk.Align.START)
		box.append(sec_label)

		ssid_label = Gtk.Label(label="SSID")
		ssid_label.set_halign(Gtk.Align.START)
		box.append(ssid_label)
		ssid_entry = Gtk.Entry()
		ssid_entry.set_text(ssid if ssid != '<hidden>' else '')
		ssid_entry.set_placeholder_text("Network name")
		box.append(ssid_entry)

		pass_entry = None
		if security != 'open':
			pass_label = Gtk.Label(label="Password")
			pass_label.set_halign(Gtk.Align.START)
			box.append(pass_label)
			pass_entry = Gtk.PasswordEntry()
			pass_entry.set_show_peek_icon(True)
# placeholder via Gtk.Entry property on the inner entry
			pass_entry.set_property("placeholder-text", f"{security} password")
			box.append(pass_entry)

		dialog.add_button("CANCEL", Gtk.ResponseType.CANCEL)
		_connect_btn = dialog.add_button("CONNECT", Gtk.ResponseType.OK)
		_connect_btn.set_margin_start(10)
		dialog.set_default_response(Gtk.ResponseType.OK)

		action_area = dialog.get_last_child()
		action_area.set_margin_end(20)
		action_area.set_margin_bottom(10)
		action_area.set_spacing(10)

# GTK4: add box to dialog manually
		dialog_box = dialog.get_child()
		content = dialog_box.get_first_child()
		content.append(box)

		def on_response(d, response):
			if response == Gtk.ResponseType.OK:
				final_ssid = ssid_entry.get_text().strip()
				password   = pass_entry.get_text().strip() if pass_entry else ''
				if not final_ssid:
					d.destroy()
					return
				threading.Thread(target=_do_connect,
					args=(final_ssid, bssid, security, password), daemon=True).start()
			d.destroy()

		dialog.connect("response", on_response)
		dialog.present()
		ssid_entry.set_position(-1)
		dialog.set_focus(pass_entry)
		return False

	def _do_connect(ssid: str, bssid: str, security: str, password: str):
		try:
			con_id = f'Auto {ssid}'

			if security == 'open':
				cmd = [
					'nmcli', 'connection', 'add',
					'type', 'wifi', 'ssid', ssid,
					'802-11-wireless.bssid', bssid,
					'connection.id', con_id,
				]
			elif security == 'WEP':
				cmd = [
					'nmcli', 'connection', 'add',
					'type', 'wifi', 'ssid', ssid,
					'802-11-wireless.bssid', bssid,
					'802-11-wireless-security.key-mgmt', 'none',
					'802-11-wireless-security.wep-key0', password,
					'connection.id', con_id,
				]
			elif security == 'WPA3':
				cmd = [
					'nmcli', 'connection', 'add',
					'type', 'wifi', 'ssid', ssid,
					'802-11-wireless.bssid', bssid,
					'802-11-wireless-security.key-mgmt', 'sae',
					'802-11-wireless-security.psk', password,
					'connection.id', con_id,
				]
			else:  # WPA, WPA2
				cmd = [
					'nmcli', 'connection', 'add',
					'type', 'wifi', 'ssid', ssid,
					'802-11-wireless.bssid', bssid,
					'802-11-wireless-security.key-mgmt', 'wpa-psk',
					'802-11-wireless-security.psk', password,
					'connection.id', con_id,
				]

# Append user-customized nmcli properties (settings dialog).
# Lines are "property value" pairs; later values override
# earlier ones with the same property since nmcli takes the
# last occurrence, so user overrides win over the defaults above.
			for line in _nmcli_extra['text'].splitlines():
				line = line.strip()
				if not line or line.startswith('#'):
					continue
				parts = line.split(None, 1)
				if len(parts) == 2:
					cmd.extend([parts[0], parts[1]])

			print(f'[*] nmcli add: {" ".join(cmd)}')
			r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
			if r.returncode != 0:
				print(f'[!] nmcli add error: {r.stderr.strip()}')
				return

			up = ['nmcli', 'connection', 'up', con_id]
			print(f'[*] nmcli up: {" ".join(up)}')
			result = subprocess.run(up, capture_output=True, text=True, timeout=30)
			if result.returncode == 0:
				print(f'[*] Connected to {ssid}')
			else:
				print(f'[!] nmcli error: {result.stderr.strip()}')
		except Exception as e:
			print(f'[!] Connect error: {e}')

	def _show_nmcli_settings(win):
		dialog = Gtk.Dialog(title="WiFi Spectrum Settings", transient_for=win, modal=True)
		dialog.set_default_size(480, 450)

		box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
		box.set_margin_top(16)
		box.set_margin_bottom(10)
		box.set_margin_start(20)
		box.set_margin_end(0)

# Dark mode selector
		theme_label = Gtk.Label(label="Dark mode")
		theme_label.set_halign(Gtk.Align.START)
		box.append(theme_label)

		theme_combo = Gtk.ComboBoxText()
		theme_combo.append('auto', 'Automatic')
		theme_combo.append('dark', 'Prefer dark mode')
		theme_combo.append('light', 'Prefer light mode')
		theme_combo.set_active_id(_theme_pref['value'])
		box.append(theme_combo)

		separator0 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
		separator0.set_margin_top(6)
		separator0.set_margin_bottom(2)
		box.append(separator0)

# Bluetooth scan switch
		bt_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
		bt_row.set_margin_top(6)
		bt_label = Gtk.Label(label="Bluetooth scan")
		bt_label.set_halign(Gtk.Align.START)
		bt_label.set_hexpand(True)
		bt_switch = Gtk.Switch()
		bt_switch.set_active(_bt_enabled['value'])
		bt_switch.set_halign(Gtk.Align.END)
		bt_row.append(bt_label)
		bt_row.append(bt_switch)
		box.append(bt_row)

		separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
		separator.set_margin_top(6)
		separator.set_margin_bottom(2)
		box.append(separator)

# Custom nmcli parameters
		info = Gtk.Label(
			label="Connection parameters (one nmcli parameter per line)\n"
					"Example: ipv6.method disabled\n"
		)
		info.set_halign(Gtk.Align.START)
		info.set_wrap(True)
		box.append(info)

		scroll = Gtk.ScrolledWindow()
		scroll.set_vexpand(True)
		textview = Gtk.TextView()
		textview.set_monospace(True)
		buf = textview.get_buffer()
		buf.set_text(_nmcli_extra['text'])
		scroll.set_child(textview)
		box.append(scroll)

		dialog.add_button("CANCEL", Gtk.ResponseType.CANCEL)
		_save_btn = dialog.add_button("SAVE", Gtk.ResponseType.OK)
		_save_btn.set_margin_start(10)
		dialog.set_default_response(Gtk.ResponseType.OK)

		action_area = dialog.get_last_child()
		action_area.set_margin_end(20)
		action_area.set_margin_bottom(10)
		action_area.set_spacing(10)

		dialog_box = dialog.get_child()
		content = dialog_box.get_first_child()
		content.append(box)

		def on_response(d, response):
			if response == Gtk.ResponseType.OK:
				start, end = buf.get_bounds()
				_nmcli_extra['text'] = buf.get_text(start, end, False)
				save_nmcli_extra(_nmcli_extra['text'])

				new_theme = theme_combo.get_active_id() or 'auto'
				if new_theme != _theme_pref['value']:
					_theme_pref['value'] = new_theme
					save_theme_pref(new_theme)
					_apply_theme(resolve_theme(new_theme))

				new_bt = bt_switch.get_active()
				if new_bt != _bt_enabled['value']:
					_bt_enabled['value'] = new_bt
					save_bluetooth_pref(new_bt)
					if new_bt:
						_start_bt_reader()
						GLib.idle_add(_apply_bt_enabled, True)
					else:
						_stop_bt_reader()
						GLib.idle_add(_hide_bt_banner)
						if _web_ref:
							GLib.idle_add(_push_update, _web_ref[0], 'updateBTDevices([])')
						GLib.idle_add(_apply_bt_enabled, False)
			d.destroy()

		dialog.connect("response", on_response)
		dialog.present()
		return False

# ----------------------------------------------------------------------
# GTK window
# ----------------------------------------------------------------------
	def activate(app):
		win = Gtk.ApplicationWindow(application=app)
		win.set_title("WiFi Spectrum Analyser")
		win.set_default_size(1600, 900)

# UserContentManager – receives JS messages
		ucm = WebKit.UserContentManager()
		ucm.register_script_message_handler("connect")

		def on_connect_msg(manager, js_result):
			print('[DEBUG] on_connect_msg fired')
			try:
# WebKit6: js_result is a JavascriptResult, value via different attrs
				if hasattr(js_result, 'get_js_value'):
					raw = js_result.get_js_value().to_string()
				elif hasattr(js_result, 'js_value'):
					raw = js_result.js_value.to_string()
				elif hasattr(js_result, 'get_value'):
					raw = js_result.get_value().to_string()
				else:
# WebKit6 passes the value object directly as js_result
					raw = js_result.to_string()
				print(f'[DEBUG] raw: {raw}')
				data = json.loads(raw)
				print(f'[DEBUG] data: {data}')
				GLib.idle_add(_show_connect_dialog, win,
					data.get('ssid', ''), data.get('bssid', ''), data.get('security', 'open'))
			except Exception as e:
				print(f'[!] Message parse error: {e}')

		ucm.connect("script-message-received::connect", on_connect_msg)

		web = WebKit.WebView(user_content_manager=ucm)
# Enable inspector for debugging (remove later)
#		settings = web.get_settings()
#		settings.set_enable_developer_extras(True)
#		web.set_settings(settings)
#		web.connect("context-menu", lambda *_: False)
# Disable context menu
		web.connect("context-menu", lambda *_: True)
		web.load_html(html, "file:///")

# ----------------------------------------------------------------------
# Status banners (rfkill / link-down) — purely passive: they
# only inform and offer an explicit action button, never act
# on their own. Live entirely in GTK, WebKit stays untouched.
# Two stacked rows: WiFi banner on top, Bluetooth banner below
# it — both radios can be flagged independently at once.
# ----------------------------------------------------------------------
		banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
		if hasattr(banner, 'add_css_class'):
			banner.add_css_class("infobar")
		banner.set_halign(Gtk.Align.FILL)
		banner.set_valign(Gtk.Align.START)
		banner.set_margin_top(10)
		banner.set_margin_start(10)
		banner.set_margin_end(10)
		banner.set_visible(False)

		banner_label = Gtk.Label(label="")
		banner_label.set_hexpand(True)
		banner_label.set_halign(Gtk.Align.START)
		banner_label.set_wrap(True)

		banner_button = Gtk.Button(label="")
		banner_button.set_visible(False)

		banner.append(banner_label)
		banner.append(banner_button)

		_banner_ref.extend([banner, banner_label, banner_button])

		bt_banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
		if hasattr(bt_banner, 'add_css_class'):
			bt_banner.add_css_class("infobar")
		bt_banner.set_halign(Gtk.Align.FILL)
		bt_banner.set_valign(Gtk.Align.START)
		bt_banner.set_margin_top(60)  # stacked below the WiFi banner row
		bt_banner.set_margin_start(10)
		bt_banner.set_margin_end(10)
		bt_banner.set_visible(False)

		bt_banner_label = Gtk.Label(label="")
		bt_banner_label.set_hexpand(True)
		bt_banner_label.set_halign(Gtk.Align.START)
		bt_banner_label.set_wrap(True)

		bt_banner_button = Gtk.Button(label="")
		bt_banner_button.set_visible(False)

		bt_banner.append(bt_banner_label)
		bt_banner.append(bt_banner_button)

		_bt_banner_ref.extend([bt_banner, bt_banner_label, bt_banner_button])

		overlay = Gtk.Overlay()
		overlay.set_child(web)
		overlay.add_overlay(banner)
		overlay.add_overlay(bt_banner)

# Small unobtrusive settings entry point — a tiny icon button
# living inside the same overlay as the banner, bottom-right corner.
		settings_btn = Gtk.Button(label="☰")
		settings_btn.set_valign(Gtk.Align.START)
		settings_btn.set_halign(Gtk.Align.END)
		settings_btn.set_margin_top(10)
		settings_btn.set_margin_end(10)
		settings_btn.set_tooltip_text("WiFi Spectrum Settings")
		settings_btn.set_opacity(0.55)
		settings_btn.connect("clicked", lambda *_: _show_nmcli_settings(win))
		if hasattr(settings_btn, 'add_css_class'):
			settings_btn.add_css_class("scan-tool-square-btn")
			_css = Gtk.CssProvider()
			_css.load_from_data(
				b".scan-tool-square-btn { border-radius: 4px; min-width: 28px; min-height: 28px; padding: 2px; }"
			)
			Gtk.StyleContext.add_provider_for_display(
				settings_btn.get_display(), _css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
			)
		overlay.add_overlay(settings_btn)

		win.set_child(overlay)
		win.present()

		_web_ref.append(web)

# React to the system theme changing at runtime while 'Automatic'
# is selected (covers switching a Cinnamon/GTK theme live, since
# WebKitGTK won't always pick this up on its own via CSS).
		def _on_system_theme_changed(*_a):
			if _theme_pref['value'] == 'auto':
				GLib.idle_add(_apply_theme, resolve_theme('auto'))

		try:
			gtk_settings = Gtk.Settings.get_default()
			if gtk_settings is not None:
				gtk_settings.connect('notify::gtk-theme-name', _on_system_theme_changed)
				gtk_settings.connect('notify::gtk-application-prefer-dark-theme', _on_system_theme_changed)
		except Exception:
			pass

# Start background threads.
# - _scan_loop: single periodic driver (interval) for BOTH the
#   WiFi scan AND the Bluetooth UI refresh.
# - _bt_reader_loop: runs continuously for the app's lifetime,
#   feeding _bt_devices with live bluetoothctl events. It does
#   NOT wait on `interval` — it just keeps reading.
		t = threading.Thread(target=_scan_loop, daemon=True)
		t.start()
		if _bt_enabled['value']:
			_start_bt_reader()

		def _on_destroy(*_a):
			_stop_flag.set()
			_bt_reader['stop'].set()

		win.connect("destroy", _on_destroy)

	app.connect("activate", activate)
	app.run([])

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
	parser = argparse.ArgumentParser(
		description="\\ scan \\  WiFi + Bluetooth Spectrum Analyser\n            😃 by Mario Lohajner 2026",
		formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument('--interface', '-i', default='', help='Wireless interface (auto-detected if omitted)')
	parser.add_argument('--interval', type=int, default=5, help='Refresh interval in seconds (default: 5)')
	args = parser.parse_args()

	iface = args.interface or detect_interface()
	print(f'[*] Interface: {iface}')

	adapter_desc = detect_adapter_info(iface)
	print(f'[*] Adapter: {adapter_desc or "unknown"}')

	theme_pref = load_theme_pref()
	theme = resolve_theme(theme_pref)
	print(f'[*] Theme: {theme_pref} -> {theme}')

	bt_enabled = load_bluetooth_pref()
	print(f'[*] Bluetooth scan: {"on" if bt_enabled else "off"}')

# Build empty template — window opens immediately, scans run in background
	html = build_html([], iface, adapter_desc, theme, bt_devices=[], bt_enabled=bt_enabled)

	show_gtk(html, iface, interval=args.interval)

if __name__ == '__main__':
	main()
