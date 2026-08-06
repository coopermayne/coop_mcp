#!/usr/bin/env python3
"""SwiftBar plugin: today's protein + water in the macOS menu bar.

  <bitbar.title>Journal Macros</bitbar.title>
  <bitbar.version>v1.0</bitbar.version>
  <bitbar.author>Cooper Mayne</bitbar.author>
  <bitbar.desc>Today's protein and water from the journal, with a full nutrient
  breakdown in the dropdown.</bitbar.desc>
  <bitbar.dependencies>python3</bitbar.dependencies>

The REFRESH INTERVAL is the filename, not anything in here: `macros.5m.py` polls
every 5 minutes. Rename to .1m./.15m. to change it; the dropdown also carries a
Refresh item for when you've just logged something and don't want to wait.

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

import json
import os
import urllib.error
import urllib.request

# --- fill these in, in your installed copy only ---------------------------- #
JOURNAL_URL = os.environ.get("JOURNAL_URL", "https://<your-host>").rstrip("/")
TOKEN = os.environ.get("JOURNAL_WIDGET_TOKEN", "")  # the server's WIDGET_TOKEN
# --------------------------------------------------------------------------- #

TIMEOUT_SECONDS = 8

# What rides in the menu bar itself. Everything else goes in the dropdown — the bar
# is glanceable or it's noise. Emoji rather than SF Symbols because SwiftBar allows
# one `sfimage` per line and this line carries two figures.
BAR = [("protein_g", "🥩", ""), ("water_oz", "💧", "")]

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


def num(v) -> str:
    """Trim a float that's really an integer: 92.0 -> 92, 1.5 -> 1.5."""
    if v is None:
        return "–"
    return str(int(v)) if float(v) == int(v) else f"{float(v):g}"


def fetch() -> dict:
    req = urllib.request.Request(f"{JOURNAL_URL}/app/api/today.json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
        return json.loads(r.read().decode())


def emit_error(short: str, detail: str) -> None:
    """Fail quietly in the bar, loudly in the dropdown. A menu bar that shouts on
    every dropped wifi connection gets ignored, and then it's useless when it
    actually matters."""
    print(f"🥩 – 💧 – | color=#888888")
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

    # --- the menu bar line ---
    bar = []
    for key, icon, _ in BAR:
        bar.append(f"{icon} {num((n.get(key) or {}).get('total'))}")
    print(" ".join(bar))

    # --- the dropdown ---
    print("---")
    print(f"Today · {payload.get('date', '?')} | color=#888888")
    for key, (suffix, label) in UNITS.items():
        info = n.get(key)
        if not info:
            continue
        total, target, ceiling = info.get("total"), info.get("target"), info.get("ceiling")
        # The unit rides with a figure, never with the "–" placeholder: "–g" reads
        # like a quantity of grams rather than "nothing logged carries this".
        line = f"{label:<9} {num(total)}{suffix if total is not None else ''}"
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
