"""Post-production pass over the recorded demo: hook, legible captions, end card.

The raw capture is a 1600px desktop UI with 27px captions. LinkedIn renders feed
video around 500px wide, so that caption lands at ~8px and the product itself is
unreadable. This pass rebuilds the presentation layer for feed viewing:

    * a 3s hook card so the first frame stops a scroll
    * caption strips at feed-legible size, overlaid over the baked-in small ones
    * punch-in zooms on the moments where numbers matter
    * an end card with the value proposition

Cards are rendered from HTML via Playwright (real typography control) rather
than ffmpeg drawtext.

Runs on the finished, voiced cut so audio stays in sync; the hook and end card
carry silence.

Usage:
    python scripts/edit_demo.py [--in=recordings/demo-linkedin.mp4]
                                [--out=recordings/demo-final.mp4]
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

RECORDINGS = Path("recordings")
CARD_DIR = RECORDINGS / "cards"
W, H = 1600, 900
HOOK_SECONDS = 3.4
END_SECONDS = 4.0
CAPTION_H = 230

# Punch-in. The app centres content in a max-w-7xl column, leaving wide dead
# margins and a nav bar that carry no information at feed size. Cropping to this
# 16:9 window and rescaling gives ~1.23x magnification of the part that matters
# and keeps the KPI row clear of the caption band.
ZOOM_CROP = (150, 95, 1300, 731)  # x, y, w, h

FONT_STACK = "'Segoe UI', system-ui, -apple-system, sans-serif"

CAPTION_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html, body { width:{W}px; height:{CH}px; background:transparent; }
  body { font-family:{FONT}; color:#fff; }
  .scrim {
    position:absolute; inset:0;
    background:linear-gradient(to top,
      rgba(2,6,23,.98) 0%, rgba(2,6,23,.96) 52%, rgba(2,6,23,.74) 78%, transparent 100%);
  }
  .inner { position:absolute; left:74px; right:74px; bottom:48px; }
  h1 { font-size:{TITLE}px; font-weight:800; letter-spacing:-.025em; line-height:1.06; }
  p  { font-size:31px; color:#cbd5e1; margin-top:12px; line-height:1.3; font-weight:500; }
  .bar { width:64px; height:5px; border-radius:999px; margin-bottom:18px;
         background:linear-gradient(90deg,#34d399,#38bdf8); }
</style></head><body>
  <div class="scrim"></div>
  <div class="inner">
    <div class="bar"></div>
    <h1>{TITLE_TEXT}</h1>
    {SUB}
  </div>
</body></html>
"""

HOOK_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { width:1600px; height:900px; overflow:hidden; background:#020617;
         font-family:FONTSTACK; color:#fff; position:relative; }
  .glow { position:absolute; inset:0; background:
      radial-gradient(circle at 84% 10%, rgba(16,185,129,.36), transparent 44%),
      radial-gradient(circle at 8% 94%, rgba(56,189,248,.30), transparent 46%); }
  .grid { position:absolute; inset:0; opacity:.15;
    background-image:
      linear-gradient(rgba(148,163,184,.6) 1px, transparent 1px),
      linear-gradient(90deg, rgba(148,163,184,.6) 1px, transparent 1px);
    background-size:60px 60px;
    mask-image:radial-gradient(circle at 50% 50%, #000 18%, transparent 76%); }
  .wrap { position:relative; height:100%; padding:0 104px;
          display:flex; flex-direction:column; justify-content:center; }
  .eyebrow { display:inline-flex; align-items:center; gap:14px; align-self:flex-start;
    border:1px solid rgba(52,211,153,.34); background:rgba(16,185,129,.12);
    color:#6ee7b7; border-radius:999px; padding:12px 26px;
    font-size:25px; font-weight:700; letter-spacing:.14em; text-transform:uppercase; }
  .dot { width:11px; height:11px; border-radius:999px; background:#34d399;
         box-shadow:0 0 16px 4px rgba(52,211,153,.75); }
  h1 { font-size:96px; line-height:1.03; letter-spacing:-.04em; font-weight:800; margin-top:34px; }
  .accent { background:linear-gradient(100deg,#34d399,#38bdf8);
            -webkit-background-clip:text; background-clip:text; color:transparent; }
  p { font-size:38px; color:#94a3b8; margin-top:28px; font-weight:500; }
</style></head><body>
  <div class="glow"></div><div class="grid"></div>
  <div class="wrap">
    <div class="eyebrow"><span class="dot"></span>Databricks &nbsp;+&nbsp; Microsoft Fabric</div>
    <h1>Same data.<br/><span class="accent">Two permission models.</span></h1>
    <p>Row filters, column masks and ABAC &mdash; kept in sync, both directions.</p>
  </div>
</body></html>
""".replace("FONTSTACK", FONT_STACK)

END_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { width:1600px; height:900px; overflow:hidden; background:#020617;
         font-family:FONTSTACK; color:#fff; position:relative; }
  .glow { position:absolute; inset:0; background:
      radial-gradient(circle at 50% 0%, rgba(16,185,129,.30), transparent 52%),
      radial-gradient(circle at 50% 100%, rgba(56,189,248,.24), transparent 52%); }
  .wrap { position:relative; height:100%; display:flex; flex-direction:column;
          align-items:center; justify-content:center; text-align:center; padding:0 96px; }
  h1 { font-size:84px; font-weight:800; letter-spacing:-.035em; line-height:1.05; }
  .accent { background:linear-gradient(100deg,#34d399,#38bdf8);
            -webkit-background-clip:text; background-clip:text; color:transparent; }
  .chips { display:flex; gap:16px; margin-top:44px; flex-wrap:nowrap; }
  .chip { border:1px solid rgba(148,163,184,.3); background:rgba(15,23,42,.8);
          border-radius:999px; padding:15px 30px; font-size:27px; font-weight:650;
          color:#e2e8f0; white-space:nowrap; }
  .chip.on { border-color:rgba(52,211,153,.45); color:#6ee7b7; background:rgba(16,185,129,.12); }
  p { font-size:32px; color:#64748b; margin-top:46px; font-weight:500; }
</style></head><body>
  <div class="glow"></div>
  <div class="wrap">
    <h1>Fabric <span class="accent">Unified Permission Hub</span></h1>
    <div class="chips">
      <span class="chip on">Bidirectional sync</span>
      <span class="chip">RLS</span>
      <span class="chip">CLS</span>
      <span class="chip">ABAC</span>
      <span class="chip on">Dry-run by default</span>
    </div>
    <p>Every change previewed, applied and audited.</p>
  </div>
</body></html>
""".replace("FONTSTACK", FONT_STACK)


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _duration(ffmpeg: str, path: Path) -> float:
    result = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, text=True)
    for line in result.stderr.splitlines():
        if "Duration:" in line:
            stamp = line.split("Duration:")[1].split(",")[0].strip()
            hours, minutes, seconds = stamp.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return 0.0


def _title_size(text: str) -> int:
    """Shrink long headlines so they stay on one or two lines."""
    length = len(text or "")
    if length <= 26:
        return 62
    if length <= 40:
        return 54
    if length <= 56:
        return 47
    return 41


def render_cards(cues: list[dict]) -> dict:
    """Render hook, end and one caption strip per cue."""
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    paths = {"captions": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)

        page = browser.new_context(viewport={"width": W, "height": H}).new_page()
        page.set_content(HOOK_HTML, wait_until="load")
        page.wait_for_timeout(350)
        paths["hook"] = CARD_DIR / "hook.png"
        page.screenshot(path=str(paths["hook"]))

        page.set_content(END_HTML, wait_until="load")
        page.wait_for_timeout(350)
        paths["end"] = CARD_DIR / "end.png"
        page.screenshot(path=str(paths["end"]))
        page.close()

        cap = browser.new_context(
            viewport={"width": W, "height": CAPTION_H}
        ).new_page()
        for index, cue in enumerate(cues):
            title = _esc(cue.get("title", ""))
            subtitle = _esc(cue.get("subtitle", ""))
            html = (
                CAPTION_HTML
                .replace("{W}", str(W))
                .replace("{CH}", str(CAPTION_H))
                .replace("{FONT}", FONT_STACK)
                .replace("{TITLE}", str(_title_size(title)))
                .replace("{TITLE_TEXT}", title)
                .replace("{SUB}", f"<p>{subtitle}</p>" if subtitle else "")
            )
            cap.set_content(html, wait_until="load")
            cap.wait_for_timeout(90)
            out = CARD_DIR / f"cap_{index:03d}.png"
            cap.screenshot(path=str(out), omit_background=True)
            paths["captions"].append(out)
        cap.close()
        browser.close()
    return paths


def main() -> int:
    args = sys.argv[1:]
    src = RECORDINGS / "demo-linkedin.mp4"
    out = RECORDINGS / "demo-final.mp4"
    music: Path | None = None
    music_gain = -12.0
    for token in args:
        if token.startswith("--in="):
            src = Path(token.split("=", 1)[1])
        elif token.startswith("--out="):
            out = Path(token.split("=", 1)[1])
        elif token.startswith("--music="):
            music = Path(token.split("=", 1)[1])
        elif token.startswith("--music-gain="):
            music_gain = float(token.split("=", 1)[1])

    cue_file = RECORDINGS / "timeline-final.json"
    if not cue_file.exists():
        print("missing recordings/timeline-final.json - run add_voiceover.py first")
        return 1
    cues = json.loads(cue_file.read_text(encoding="utf-8"))["cues"]
    if not src.exists():
        print(f"source video not found: {src}")
        return 1

    print(f"source   : {src.name}")
    print(f"cues     : {len(cues)}")
    print("rendering cards...")
    cards = render_cards(cues)

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    # --- pass 1: overlay legible caption strips over the baked-in small ones ---
    captioned = RECORDINGS / "_captioned.mp4"
    command = [ffmpeg, "-y", "-i", str(src)]
    for path in cards["captions"]:
        command += ["-i", str(path)]

    steps = []
    zx, zy, zw, zh = ZOOM_CROP
    steps.append(f"[0:v]crop={zw}:{zh}:{zx}:{zy},scale={W}:{H},setsar=1[z]")
    current = "z"
    for index, cue in enumerate(cues):
        start = float(cue["start"])
        end = start + float(cue["duration"]) + 0.5
        label = f"c{index}"
        steps.append(
            f"[{current}][{index + 1}:v]overlay=0:{H - CAPTION_H}:"
            f"enable='between(t,{start:.2f},{end:.2f})'[{label}]"
        )
        current = label
    command += [
        "-filter_complex", ";".join(steps),
        "-map", f"[{current}]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        str(captioned),
    ]
    print("overlaying captions...")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print("\n".join(result.stderr.strip().splitlines()[-12:]))
        return 1

    # --- pass 2: hook card + body + end card ---
    print("adding hook and end cards...")
    command = [
        ffmpeg, "-y",
        "-loop", "1", "-t", str(HOOK_SECONDS), "-i", str(cards["hook"]),
        "-i", str(captioned),
        "-loop", "1", "-t", str(END_SECONDS), "-i", str(cards["end"]),
        "-f", "lavfi", "-t", str(HOOK_SECONDS), "-i", "anullsrc=r=48000:cl=stereo",
        "-f", "lavfi", "-t", str(END_SECONDS), "-i", "anullsrc=r=48000:cl=stereo",
    ]
    music_index = None
    if music and music.exists():
        music_index = 5
        command += ["-i", str(music)]
    elif music:
        print(f"music file not found, continuing without it: {music}")

    graph = (
        f"[0:v]scale={W}:{H},setsar=1,fps=30,format=yuv420p,"
        f"fade=t=out:st={HOOK_SECONDS - 0.5}:d=0.5[hv];"
        f"[1:v]scale={W}:{H},setsar=1,fps=30,format=yuv420p,fade=t=in:st=0:d=0.4[bv];"
        f"[2:v]scale={W}:{H},setsar=1,fps=30,format=yuv420p,fade=t=in:st=0:d=0.5[ev];"
        "[3:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[ha];"
        "[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[ba];"
        "[4:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[ea];"
    )
    if music_index is None:
        graph += "[hv][ha][bv][ba][ev][ea]concat=n=3:v=1:a=1[v][a]"
    else:
        total = HOOK_SECONDS + _duration(ffmpeg, captioned) + END_SECONDS
        graph += (
            "[hv][ha][bv][ba][ev][ea]concat=n=3:v=1:a=1[v][speech];"
            "[speech]asplit=2[speech_out][keyed];"
            # Bed runs under the hook and end card too - a silent opening is
            # the fastest way to lose a scroll.
            f"[{music_index}:a]aloop=loop=-1:size=2e9,aresample=48000,"
            f"aformat=channel_layouts=stereo,volume={music_gain}dB,"
            f"afade=t=in:st=0:d=1.0,afade=t=out:st={max(0.0, total - 2.2):.2f}:d=2.2[bed];"
            "[bed][keyed]sidechaincompress=threshold=0.03:ratio=8:"
            "attack=25:release=450[ducked];"
            "[speech_out][ducked]amix=inputs=2:normalize=0:"
            "duration=first:dropout_transition=0[a]"
        )

    command += [
        "-filter_complex", graph,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        str(out),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print("\n".join(result.stderr.strip().splitlines()[-15:]))
        return 1
    captioned.unlink(missing_ok=True)

    print(f"\nwrote {out.resolve()}")
    print(f"  size    : {out.stat().st_size / 1_000_000:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
