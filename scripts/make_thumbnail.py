"""Render LinkedIn thumbnails for the demo video.

Draws a designed card with Playwright and screenshots it, rather than grabbing a
video frame - UI screenshots are unreadable at feed size. Text is kept large
because LinkedIn renders thumbnails small on mobile.

Outputs:
    recordings/thumbnail.png        1280x720  video cover / 16:9 post image
    recordings/thumbnail-link.png   1200x627  link-preview aspect

Usage:
    python scripts/make_thumbnail.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright

OUT_DIR = Path("recordings")

TEMPLATE = """
<!doctype html>
<html><head><meta charset="utf-8"/>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    width: {W}px; height: {H}px; overflow: hidden;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: #020617; color: #fff; position: relative;
  }
  .glow {
    position:absolute; inset:0;
    background:
      radial-gradient(circle at 88% 8%, rgba(16,185,129,.34), transparent 42%),
      radial-gradient(circle at 6% 96%, rgba(56,189,248,.28), transparent 44%);
  }
  .grid {
    position:absolute; inset:0; opacity:.16;
    background-image:
      linear-gradient(rgba(148,163,184,.6) 1px, transparent 1px),
      linear-gradient(90deg, rgba(148,163,184,.6) 1px, transparent 1px);
    background-size: 56px 56px;
    mask-image: radial-gradient(circle at 50% 50%, #000 20%, transparent 78%);
  }
  .wrap { position:relative; height:100%; padding: {PAD}px {PADX}px; display:flex;
          flex-direction:column; justify-content:center; }
  .eyebrow {
    display:inline-flex; align-items:center; gap:12px; align-self:flex-start;
    border:1px solid rgba(52,211,153,.32); background:rgba(16,185,129,.12);
    color:#6ee7b7; border-radius:999px; padding:9px 20px;
    font-size:{EYEBROW}px; font-weight:650; letter-spacing:.13em; text-transform:uppercase;
  }
  .dot { width:9px; height:9px; border-radius:999px; background:#34d399;
         box-shadow:0 0 14px 3px rgba(52,211,153,.75); }
  h1 { font-size:{TITLE}px; line-height:1.02; letter-spacing:-.035em; font-weight:800;
       margin-top:{GAP}px; }
  h1 .accent { background:linear-gradient(100deg,#34d399,#38bdf8);
               -webkit-background-clip:text; background-clip:text; color:transparent; }
  .flow { display:flex; align-items:center; gap:{FGAP}px; margin-top:{GAP}px; flex-wrap:nowrap; }
  .node { border:1px solid rgba(148,163,184,.26); background:rgba(15,23,42,.82);
          border-radius:16px; padding:{NPAD}px {NPADX}px; }
  .node .k { font-size:{NK}px; color:#64748b; letter-spacing:.1em;
             text-transform:uppercase; font-weight:650; }
  .node .v { font-size:{NV}px; font-weight:700; margin-top:5px; white-space:nowrap; }
  .arrows { font-size:{ARROW}px; color:#34d399; font-weight:800; line-height:1;
            text-align:center; letter-spacing:-.06em; }
    .chips { display:flex; gap:{CGAP}px; margin-top:{GAP}px; flex-wrap:nowrap; }
  .chip { border:1px solid rgba(148,163,184,.24); background:rgba(148,163,184,.09);
          color:#cbd5e1; border-radius:999px; padding:8px 18px;
          font-size:{CHIP}px; font-weight:600; }
  .chip.hot { border-color:rgba(52,211,153,.45); background:rgba(16,185,129,.14); color:#6ee7b7; }
</style></head>
<body>
  <div class="glow"></div><div class="grid"></div>
  <div class="wrap">
    <div class="eyebrow"><span class="dot"></span>Live demo</div>
    <h1>One control plane for<br/><span class="accent">Databricks + Fabric</span> permissions</h1>
    <div class="flow">
      <div class="node"><div class="k">Source</div><div class="v">Unity Catalog</div></div>
      <div class="arrows">&#8644;</div>
      <div class="node"><div class="k">Target</div><div class="v">Fabric OneLake</div></div>
    </div>
    <div class="chips">
      <span class="chip hot">Row-level security</span>
      <span class="chip hot">Column-level security</span>
      <span class="chip hot">ABAC policies</span>
      <span class="chip">Bidirectional</span>
      <span class="chip">Dry-run first</span>
      <span class="chip">Audited</span>
    </div>
  </div>
</body></html>
"""

VARIANTS = [
    # name,                 W,    H,   pad, padx, title, eyebrow, gap, chip, nk, nv, npad, npadx, arrow, fgap, cgap
    ("thumbnail.png",      1280, 720,  0,   72,   60,    15,      28,  18,   14, 27, 17,   26,    46,    22,   10),
    ("thumbnail-link.png", 1200, 627,  0,   64,   54,    14,      24,  17,   13, 25, 15,   23,    42,    20,   9),
]


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for (name, w, h, pad, padx, title, eyebrow, gap,
             chip, nk, nv, npad, npadx, arrow, fgap, cgap) in VARIANTS:
            html = (TEMPLATE
                    .replace("{W}", str(w)).replace("{H}", str(h))
                    .replace("{PADX}", str(padx)).replace("{PAD}", str(pad))
                    .replace("{TITLE}", str(title)).replace("{EYEBROW}", str(eyebrow))
                    .replace("{GAP}", str(gap)).replace("{CHIP}", str(chip))
                    .replace("{NK}", str(nk)).replace("{NV}", str(nv))
                    .replace("{NPADX}", str(npadx)).replace("{NPAD}", str(npad))
                    .replace("{ARROW}", str(arrow)).replace("{FGAP}", str(fgap))
                    .replace("{CGAP}", str(cgap)))
            page = browser.new_context(
                viewport={"width": w, "height": h}, device_scale_factor=2
            ).new_page()
            page.set_content(html)
            page.wait_for_timeout(400)
            target = OUT_DIR / name
            page.screenshot(path=str(target))
            print(f"wrote {target}  ({w}x{h} @2x, {target.stat().st_size/1000:.0f} KB)")
            page.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
