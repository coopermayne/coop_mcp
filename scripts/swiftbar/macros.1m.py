#!/usr/bin/env python3
"""SwiftBar plugin: today's protein + water in the macOS menu bar.

  <bitbar.title>Journal Macros</bitbar.title>
  <bitbar.version>v1.0</bitbar.version>
  <bitbar.author>Cooper Mayne</bitbar.author>
  <bitbar.desc>Today's protein and water from the journal, with a full nutrient
  breakdown in the dropdown.</bitbar.desc>
  <bitbar.dependencies>python3</bitbar.dependencies>

The REFRESH INTERVAL is the filename, not anything in here: `macros.1m.py` polls
every minute. Rename to .5m./.15m. to change it. Don't go much below a minute: the
figures only move when you LOG something, a handful of times a day, so a tighter
poll buys nothing and just spawns a process every few seconds — and for the one
moment it would help (right after logging) the dropdown's Refresh item is instant.

Setup lives in the repo README ("Menu-bar macros"). Fill in JOURNAL_URL and TOKEN
below — but do that in your INSTALLED COPY, not in this file.

  THIS REPO IS PUBLIC. A token committed here is a published token, and git history
  keeps it even after a later commit deletes the line. So the file you actually run
  is a plain copy in your SwiftBar plugin folder (outside the repo) with the real
  values filled in; this tracked version stays a template with placeholders. The
  trade is deliberate: a symlink would auto-update on `git pull`, but it would also
  mean the file you edit is the file you commit — one `git add -A` from publishing
  the token. Copy, re-copy when this changes.
"""

import hashlib
import json
import os
import pathlib
import ssl
import subprocess
import urllib.error
import urllib.request

# --- fill these in, in your installed copy only ---------------------------- #
JOURNAL_URL = os.environ.get("JOURNAL_URL", "https://<your-host>").rstrip("/")
TOKEN = os.environ.get("JOURNAL_WIDGET_TOKEN", "")  # the server's WIDGET_TOKEN
# --------------------------------------------------------------------------- #

TIMEOUT_SECONDS = 8

# What rides in the menu bar, and the short label drawn above each figure. Everything
# else goes in the dropdown — the bar is glanceable or it's noise. Uppercase to match
# the CPU/RAM/SSD blocks these sit beside: matching case is most of what makes the
# whole row read as one set of readouts rather than two apps sharing a menu bar.
BAR = [("protein_g", "PRT"), ("water_oz", "WTR")]

# Gauge geometry. SEGMENTS is how many cells each bar has — 7 means one cell is
# ~14%, which is about the finest distinction worth making at a glance. The menu bar
# line is rendered in a MONOSPACE font so the two gauges stay the same width and the
# fill level is comparable between them; in a proportional font they'd drift.
SEGMENTS = 7
BAR_FONT = "Menlo"
FILLED, EMPTY, UNLOGGED = "█", "░", "┈"

# Display units — deliberately client-side. The API returns bare numbers because
# units are a rendering choice that already lives in the web app's templates, and a
# second server-side copy is how the two drift apart.
UNITS = {
    "calories": ("", "Calories"),
    "protein_g": ("g", "Protein"),
    "carbs_g": ("g", "Carbs"),
    "fat_g": ("g", "Fat"),
    "sodium_mg": ("mg", "Sodium"),
    "fiber_g": ("g", "Fiber"),
    "water_oz": ("oz", "Water"),
    "standard_drinks": ("", "Drinks"),
}


# --------------------------------------------------------------------------- #
# The menu-bar image
#
# SwiftBar's title is ONE line of text, and multiple title lines cycle rather than
# stack — so the Stats-style "small label above a big figure" layout can only be an
# image. This renders one: AppKit draws it, so the type is the system font at the
# system's own hinting rather than an approximation.
#
# It's a TEMPLATE image (black + alpha), which is what makes it look native rather
# than pasted on: macOS tints template images itself, so it follows light/dark mode
# and inverts correctly when the menu is open. The cost is that it can't carry
# colour, so over/under-target colouring lives in the dropdown instead.
#
# Compiled ONCE into a cache dir and reused — `swiftc` takes ~5s, which is fine at
# install time and absurd once a minute. The binary is keyed by a hash of the
# source below, so editing the layout recompiles automatically.
# --------------------------------------------------------------------------- #

RENDERER_SWIFT = r'''
import AppKit

// These match exelban/stats' Mini widget (Kit/Widgets/Mini.swift) so this sits in
// the menu bar as a peer of the CPU/RAM/SSD blocks rather than an approximation of
// them: 7pt light label at y=12, 12pt regular value at y=1, in a fixed-width column.
// The fixed width is the important one — a column sized to its text makes the whole
// group breathe in and out as figures change width, which is what gave the first
// attempt its "close but not quite" feel next to the real thing.
let SCALE: CGFloat = 2          // retina
let H: CGFloat = 20             // Constants.Widget.height minus vertical margins
let LABEL_SIZE: CGFloat = 7
let VALUE_SIZE: CGFloat = 12
let COL_WIDTH: CGFloat = 31
let COL_GAP: CGFloat = 4
let LABEL_Y: CGFloat = 12
let VALUE_Y: CGFloat = 1
let PAD: CGFloat = 1

struct Col { let label: String; let value: String }

let cols: [Col] = CommandLine.arguments.dropFirst().map { arg in
    let parts = arg.split(separator: ":", maxSplits: 1).map(String.init)
    return Col(label: parts.first ?? "", value: parts.count > 1 ? parts[1] : "")
}
guard !cols.isEmpty else { exit(1) }

let labelAttrs: [NSAttributedString.Key: Any] = [
    .font: NSFont.systemFont(ofSize: LABEL_SIZE, weight: .light),
    .foregroundColor: NSColor.black,
]
// Monospaced DIGITS at Stats' weight: it renders the same as the system font at this
// size, but pins the digits to one advance width so a figure ticking 99%->100% can't
// jiggle the text inside its column.
let valueAttrs: [NSAttributedString.Key: Any] = [
    .font: NSFont.monospacedDigitSystemFont(ofSize: VALUE_SIZE, weight: .regular),
    .foregroundColor: NSColor.black,
]

// Fixed column width, like Stats — every block is the same size whatever it holds,
// so the group's overall width never changes and neighbouring menu bar items don't
// shuffle sideways when a number does. A value wider than the column (shouldn't
// happen under 1000%) just centres and overhangs rather than clipping.
let W = COL_WIDTH * CGFloat(cols.count) + COL_GAP * CGFloat(cols.count - 1) + PAD * 2

let pxW = Int(ceil(W * SCALE)), pxH = Int(ceil(H * SCALE))
guard let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: pxW, pixelsHigh: pxH,
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { exit(1) }
rep.size = NSSize(width: W, height: H)

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

var x = PAD
for c in cols {
    let ls = (c.label as NSString).size(withAttributes: labelAttrs)
    let vs = (c.value as NSString).size(withAttributes: valueAttrs)
    // AppKit's origin is bottom-left, so the label's larger y puts it on top.
    (c.label as NSString).draw(at: NSPoint(x: x + (COL_WIDTH - ls.width) / 2, y: LABEL_Y),
                               withAttributes: labelAttrs)
    (c.value as NSString).draw(at: NSPoint(x: x + (COL_WIDTH - vs.width) / 2, y: VALUE_Y),
                               withAttributes: valueAttrs)
    x += COL_WIDTH + COL_GAP
}

NSGraphicsContext.restoreGraphicsState()

guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
print(png.base64EncodedString())
'''


def renderer_binary() -> "pathlib.Path | None":
    """Path to the compiled renderer, building it on first use. None if unavailable."""
    cache = pathlib.Path.home() / "Library/Caches/journal-macros"
    stamp = hashlib.sha256(RENDERER_SWIFT.encode()).hexdigest()[:12]
    binary = cache / f"macrobar-{stamp}"
    if binary.exists():
        return binary
    try:
        cache.mkdir(parents=True, exist_ok=True)
        for stale in cache.glob("macrobar-*"):   # a previous layout's build
            stale.unlink()
        src = cache / f"macrobar-{stamp}.swift"
        src.write_text(RENDERER_SWIFT)
        subprocess.run(["swiftc", "-O", "-o", str(binary), str(src)],
                       capture_output=True, timeout=120, check=True)
        src.unlink(missing_ok=True)
        return binary
    except Exception:
        return None      # no Xcode CLT, or the build failed — caller falls back to text


def bar_image(columns) -> "str | None":
    """Base64 PNG for the menu bar, or None to fall back to text."""
    binary = renderer_binary()
    if not binary:
        return None
    try:
        out = subprocess.run([str(binary), *[f"{lbl}:{val}" for lbl, val in columns]],
                             capture_output=True, text=True, timeout=15, check=True)
        return out.stdout.strip() or None
    except Exception:
        return None


def tls_context() -> ssl.SSLContext:
    """A verifying SSL context that works whichever python3 SwiftBar picks up.

    A python.org build ships no CA bundle of its own (`ssl.get_default_verify_paths()
    .cafile` is None until you run its "Install Certificates.command"), so HTTPS fails
    with CERTIFICATE_VERIFY_FAILED even though `curl` — which uses the system trust
    store — works fine from the same shell. Apple's /usr/bin/python3 and Homebrew's
    are both fine. Since SwiftBar resolves `python3` off its OWN PATH, which build
    runs here isn't something this script can assume; preferring certifi's bundle when
    it's importable fixes every case without touching the interpreter.

    Note this still VERIFIES — it never falls back to an unverified context. A menu-bar
    macro count is not worth teaching a script to accept any certificate.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def gauge(total, target, segments: int = SEGMENTS) -> str:
    """A battery-style fill bar: how much of `target` `total` covers.

    Three states, because "nothing logged" and "logged, still at zero" are different
    facts and the journal's rings already distinguish them. A nutrient nothing has
    reported (total is None) draws DASHED — an empty solid bar would claim you're at
    0% of the goal, when really nobody has estimated it yet. No target at all (fat)
    draws blank: there's no goal to be a fraction of.

    Overshoot CAPS at full rather than wrapping or overflowing the width, the same
    way the rings cap their arc at a full circle — past 100% the interesting fact is
    "you're there", and a bar that kept growing would just break the alignment.

    Rounds DOWN, with a floor of one cell for any non-zero amount. Rounding to nearest
    fills the last cell at ~93%, so a full bar would mean "nearly there" — exactly the
    reading that makes you stop short of a goal you haven't met. Down-rounding means a
    completely full bar happens only at 100%, and the one-cell floor keeps a small
    first log from looking like nothing was logged at all.
    """
    if not target:
        return " " * segments
    if total is None:
        return UNLOGGED * segments
    frac = max(0.0, total / target)
    filled = segments if frac >= 1 else max(1 if frac > 0 else 0, int(frac * segments))
    return FILLED * filled + EMPTY * (segments - filled)


def num(v) -> str:
    """Trim a float that's really an integer: 92.0 -> 92, 1.5 -> 1.5."""
    if v is None:
        return "–"
    return str(int(v)) if float(v) == int(v) else f"{float(v):g}"


def fetch() -> dict:
    req = urllib.request.Request(f"{JOURNAL_URL}/app/api/today.json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS,
                                context=tls_context()) as r:
        return json.loads(r.read().decode())


def emit_error(short: str, detail: str) -> None:
    """Fail quietly in the bar, loudly in the dropdown. A menu bar that shouts on
    every dropped wifi connection gets ignored, and then it's useless when it
    actually matters.

    Keeps the normal layout with "–" in place of each figure rather than collapsing
    to an error string: the block stays the same width, so a blip doesn't shove every
    neighbouring menu bar item sideways and back.
    """
    dashes = [(label, "–") for _, label in BAR]
    img = bar_image(dashes)
    if img:
        print(f" | templateImage={img}")
    else:
        print("  ".join(f"{label} {UNLOGGED * SEGMENTS}" for _, label in BAR)
              + f" | font={BAR_FONT} color=#888888")
    print("---")
    print(f"{short} | color=red")
    for line in detail.splitlines():
        print(f"{line} | color=#888888 font=Menlo size=11")
    print("---")
    print("Refresh | refresh=true")


def main() -> None:
    try:
        payload = fetch()
    except urllib.error.HTTPError as e:
        hint = ("Token rejected — check TOKEN in this script matches WIDGET_TOKEN "
                "on the server." if e.code == 401 else f"HTTP {e.code} from the journal.")
        return emit_error("Journal: not authorized" if e.code == 401
                          else f"Journal: HTTP {e.code}", hint)
    except Exception as e:
        return emit_error("Journal: unreachable", f"{type(e).__name__}: {e}")

    n = payload.get("nutrients", {})

    # --- the menu bar: percent-of-target per metric, Stats-style ---
    columns = []
    for key, label in BAR:
        info = n.get(key) or {}
        total, target = info.get("total"), info.get("target")
        # "–" rather than "0%" when nothing logged carries it: the honest reading of
        # an unestimated nutrient, and the same distinction the rings draw.
        pct = "–" if (total is None or not target) else f"{round(total / target * 100)}%"
        columns.append((label, pct))

    img = bar_image(columns)
    if img:
        print(f" | templateImage={img}")
    else:
        # No Swift toolchain (or the render failed) — the block gauges still say the
        # same thing in plain text, so the plugin degrades instead of going blank.
        gauges = [f"{lbl} {gauge((n.get(k) or {}).get('total'), (n.get(k) or {}).get('target'))}"
                  for (k, lbl) in BAR]
        print(f"{'  '.join(gauges)} | font={BAR_FONT}")

    # --- the dropdown: the same gauges, with the figures the bar drops ---
    print("---")
    print(f"Today · {payload.get('date', '?')} | color=#888888")
    for key, (suffix, label) in UNITS.items():
        info = n.get(key)
        if not info:
            continue
        total, target, ceiling = info.get("total"), info.get("target"), info.get("ceiling")
        # The unit rides with a figure, never with the "–" placeholder: "–g" reads
        # like a quantity of grams rather than "nothing logged carries this".
        line = (f"{label:<9} {gauge(total, target)}  "
                f"{num(total)}{suffix if total is not None else ''}")
        if target is not None:
            line += f" / {num(target)}{suffix}"
        # Only a CEILING can be violated by going up (sodium, calories, alcohol); a
        # floor just isn't met yet, and colouring "not yet" red all morning would
        # train you to ignore the colour.
        color = ""
        if total is not None and target:
            if ceiling and total > target:
                color = " | color=red"
            elif not ceiling and total >= target:
                color = " | color=green"
        print(f"{line}{color} | font=Menlo size=12")
    print("---")
    print(f"Open journal | href={JOURNAL_URL}/app/journal")
    print("Refresh | refresh=true")


if __name__ == "__main__":
    main()
