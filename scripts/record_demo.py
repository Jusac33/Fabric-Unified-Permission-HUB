"""Record an end-to-end permission-sync demo as a video.

Drives the running UPH app with Playwright, narrating each step with an
on-screen caption, and captures both sync directions:

    Act 1-4  Unity Catalog -> Fabric   (Fabric starts empty)
    Act 5-8  Fabric -> Unity Catalog   (UC grants cleared mid-take)

Between the two halves the script clears the *source* side for the reverse
direction, because a sync only propagates rows that exist on its source.

Prerequisite: the app must already be running, e.g.
    uvicorn app.main:app --host 127.0.0.1 --port 8011

Usage:
    python scripts/record_demo.py <pairing_id> [--base=http://127.0.0.1:8011]
                                               [--live]   # perform real applies
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright

VIDEO_DIR = Path("recordings")
SIZE = {"width": 1600, "height": 900}

# Measured narration lengths, written by scripts/add_voiceover.py. A caption
# must stay on screen for at least as long as its line takes to speak, or the
# voiceover for one cue runs over the next.
DURATION_CACHE = VIDEO_DIR / "narration_durations.json"
SPEECH_PAD = 0.7
WORDS_PER_SECOND = 2.6  # fallback estimate for unseen lines


def _load_narration_durations() -> dict:
    try:
        return json.loads(DURATION_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _narration_key(text: str) -> str:
    return hashlib.sha1(" ".join((text or "").split()).encode("utf-8")).hexdigest()[:16]

TICK_JS = r"""
(() => {
  const install = () => {
    if (!document.body || document.getElementById('uph-demo-tick')) return;
    const style = document.createElement('style');
    style.textContent =
      '@keyframes uph-tick { 0% { opacity: .25 } 50% { opacity: 1 } 100% { opacity: .25 } }';
    document.head.appendChild(style);
    const tick = document.createElement('div');
    tick.id = 'uph-demo-tick';
    tick.style.cssText = [
      'position:fixed','left:0','top:0','width:2px','height:2px','z-index:2147483647',
      'background:#020617','pointer-events:none','animation:uph-tick .5s linear infinite'
    ].join(';');
    document.body.appendChild(tick);
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', install);
  } else {
    install();
  }
})()
"""

CAPTION_JS = """
([title, subtitle]) => {
  let el = document.getElementById('uph-demo-caption');
  if (!el) {
    el = document.createElement('div');
    el.id = 'uph-demo-caption';
    el.style.cssText = [
      'position:fixed','left:0','right:0','bottom:0','z-index:2147483647',
      'padding:22px 40px','font-family:Segoe UI,system-ui,sans-serif',
      'background:linear-gradient(to top, rgba(2,6,23,.97), rgba(2,6,23,.86) 70%, transparent)',
      'color:#fff','transition:opacity .4s ease','pointer-events:none'
    ].join(';');
    document.body.appendChild(el);
  }
  el.innerHTML =
    '<div style="font-size:27px;font-weight:700;letter-spacing:-.02em">' + title + '</div>' +
    (subtitle ? '<div style="font-size:17px;color:#94a3b8;margin-top:5px">' + subtitle + '</div>' : '');
  el.style.opacity = '1';
}
"""


HIGHLIGHT_JS = """
(el) => {
  document.querySelectorAll('[data-uph-hl]').forEach(e => {
    e.style.outline = ''; e.style.background = ''; e.removeAttribute('data-uph-hl');
  });
  el.setAttribute('data-uph-hl', '1');
  // outline + tint only: a large box-shadow spotlight gets clipped by the
  // scrollable table containers on the diff page.
  el.style.outline = '3px solid #10b981';
  el.style.outlineOffset = '3px';
  el.style.borderRadius = '8px';
  el.style.background = 'rgba(16,185,129,.16)';
  return true;
}
"""

CLEAR_HIGHLIGHT_JS = """
() => document.querySelectorAll('[data-uph-hl]').forEach(e => {
  e.style.outline = ''; e.style.background = ''; e.removeAttribute('data-uph-hl');
})
"""

# Redacts real addresses before they are ever painted to a frame. Installed as an
# init script so it survives navigation, with a MutationObserver to catch HTMX
# partial swaps. Purely cosmetic - the app, the APIs and the audit log are
# untouched.
MASK_EMAILS_JS = r"""
(() => {
  const PATTERN = '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}';
  const mask = (s) => s.replace(new RegExp(PATTERN, 'g'), (m) => {
    const at = m.indexOf('@');
    const user = m.slice(0, at);
    const head = user.slice(0, 2);
    const dots = '\u2022'.repeat(Math.max(3, user.length - 2));
    return head + dots + '@' + m.slice(at + 1);
  });
  const hasEmail = (s) => new RegExp(PATTERN).test(s);
  const walk = () => {
    if (!document.body) return;
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const hits = [];
    while (w.nextNode()) {
      if (hasEmail(w.currentNode.nodeValue)) hits.push(w.currentNode);
    }
    hits.forEach((n) => { n.nodeValue = mask(n.nodeValue); });
    document.querySelectorAll('[title]').forEach((el) => {
      const t = el.getAttribute('title');
      if (t && hasEmail(t)) el.setAttribute('title', mask(t));
    });
  };
  const start = () => {
    walk();
    new MutationObserver(walk).observe(document.body, {childList: true, subtree: true});
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})()
"""


class Recorder:
    def __init__(self, page, base: str, started_at: float | None = None):
        self.page = page
        self.base = base.rstrip("/")
        self.started_at = started_at if started_at is not None else time.time()
        self.timeline: list[dict] = []
        self.narration_durations = _load_narration_durations()

    def _speech_seconds(self, text: str) -> float:
        if not text:
            return 0.0
        known = self.narration_durations.get(_narration_key(text))
        if known:
            return float(known)
        return len(text.split()) / WORDS_PER_SECOND

    def _safe_evaluate(self, script: str, arg=None) -> bool:
        """Evaluate, tolerating an in-flight navigation destroying the context."""
        for attempt in (1, 2):
            try:
                if arg is None:
                    self.page.evaluate(script)
                else:
                    self.page.evaluate(script, arg)
                return True
            except Exception:
                if attempt == 1:
                    try:
                        self.page.wait_for_load_state("networkidle", timeout=120000)
                    except Exception:
                        pass
                    self.page.wait_for_timeout(800)
        return False

    def caption(self, title: str, subtitle: str = "", hold: float = 3.0,
                say: str | None = None) -> None:
        """Show a caption and log it for voiceover synchronisation.

        ``say`` is the spoken line; it defaults to the on-screen text but is
        usually written more conversationally. The caption is held for at least
        as long as the line takes to speak so the narration never overlaps the
        next cue.
        """
        spoken = say if say is not None else f"{title}. {subtitle}".strip(". ")
        hold = max(hold, self._speech_seconds(spoken) + SPEECH_PAD)
        self.timeline.append({
            "t": round(time.time() - self.started_at, 2),
            "title": title,
            "subtitle": subtitle,
            "say": spoken,
            "hold": round(hold, 2),
        })
        self._safe_evaluate(CAPTION_JS, [title, subtitle])
        self.page.wait_for_timeout(int(hold * 1000))

    def goto(self, path: str) -> None:
        self.page.goto(f"{self.base}{path}", wait_until="networkidle")
        self.page.wait_for_timeout(1200)

    def scroll_to(self, selector: str) -> None:
        try:
            self.page.locator(selector).first.scroll_into_view_if_needed(timeout=8000)
            self.page.wait_for_timeout(900)
        except Exception:
            pass

    def expand(self, text: str) -> bool:
        """Open the collapsible group whose summary contains ``text``."""
        try:
            summary = self.page.locator(f'summary:has-text("{text}")').first
            summary.scroll_into_view_if_needed(timeout=8000)
            summary.click(timeout=8000)
            self.page.wait_for_timeout(1100)
            return True
        except Exception:
            return False

    def highlight(self, selector: str, hold: float = 0.8) -> bool:
        """Spotlight the first element matching a Playwright selector."""
        try:
            element = self.page.locator(selector).first
            element.scroll_into_view_if_needed(timeout=8000)
            self.page.wait_for_timeout(500)
            element.evaluate(HIGHLIGHT_JS)
            self.page.wait_for_timeout(int(hold * 1000))
            return True
        except Exception:
            return False

    def clear_highlight(self) -> None:
        self._safe_evaluate(CLEAR_HIGHLIGHT_JS)
        self.page.wait_for_timeout(400)

    def submit(self, form_selector: str, button: str, wait_ms: int = 600000) -> None:
        """Click a submit button whose apply may outlast the click timeout.

        A real apply issues dozens of Graph/Fabric calls, so the navigation can
        take minutes. The click is allowed to time out waiting for navigation;
        the load state is then awaited separately.
        """
        try:
            self.page.locator(form_selector).locator(button).first.click(timeout=60000)
        except Exception:
            pass
        self.page.wait_for_load_state("networkidle", timeout=wait_ms)
        self.page.wait_for_timeout(1500)


def _clear_side(pairing_id: str, side: str) -> None:
    """Reset one platform so the reverse direction has something to propagate."""
    from scripts.permission_reset import cleanup

    cleanup(pairing_id, apply=True, include_system=False, side=side)


def run(pairing_id: str, base: str, live: bool) -> int:
    VIDEO_DIR.mkdir(exist_ok=True)
    direction_form = 'form:has(input[name="direction"][value="{}"])'
    PREVIEW_BUTTON = 'button:has-text("Preview (dry run)")'
    SYNC_BUTTON = 'button:has-text("Sync selected")'

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--force-device-scale-factor=1"])

        # Establish the starting state BEFORE the recording context exists.
        # Clearing Fabric is a REST-only step with no page open, which would
        # otherwise be captured as a long, frame-starved dead stretch.
        if live:
            _clear_side(pairing_id, "fabric")

        context = browser.new_context(
            viewport=SIZE, record_video_dir=str(VIDEO_DIR), record_video_size=SIZE
        )
        # Video capture begins when the context is created, so the voiceover
        # clock must start here for cues to line up.
        started_at = time.time()
        page = context.new_page()
        page.add_init_script(MASK_EMAILS_JS)
        page.add_init_script(TICK_JS)
        page.on("dialog", lambda d: d.accept())
        rec = Recorder(page, base, started_at)

        # ---------- Act 1: the problem ----------
        rec.goto("/")
        rec.caption("Fabric Unified Permission Hub",
                    "One control plane for Databricks Unity Catalog and Microsoft Fabric", 4.5,
                    say="This is the Fabric Unified Permission Hub, a single control plane "
                        "for permissions across Databricks Unity Catalog and Microsoft Fabric.")
        rec.caption("Starting state: Fabric has no OneLake permissions",
                    "Unity Catalog holds the grants. Fabric OneLake has none.", 5,
                    say="Right now Unity Catalog holds all of the grants, and Fabric OneLake "
                        "has none. Everything shown as drift is a permission that has not "
                        "made it across yet.")

        # ---------- Act 2: the diff ----------
        rec.goto(f"/pairings/{pairing_id}?refresh=1")
        rec.caption("Every permission, side by side",
                    "Grants, row filters, column masks and ABAC policies in one diff", 5,
                    say="The hub compares both platforms and lays every permission out side "
                        "by side. Grants, row filters, column masks and attribute based "
                        "policies, all in a single diff.")
        rec.scroll_to(direction_form.format("dbx_to_fabric"))
        rec.caption("Unity Catalog -> Fabric",
                    "These permissions exist only in Databricks", 4.5,
                    say="These permissions exist only in Databricks. Let's look at what they "
                        "actually contain.")

        # ---------- Act 2a: row-level security ----------
        rec.caption("Not just grants: row-level security",
                    "Unity Catalog row filters become Fabric OneLake row constraints", 4,
                    say="And this is not just table grants. Row level security comes across too.")
        if rec.expand("Row-level security"):
            rec.highlight('details:has(summary:has-text("Row-level security"))', 1.0)
            rec.caption("Row-level security (RLS)",
                        "Each filter is translated, not just copied", 5,
                        say="Each Unity Catalog row filter is translated into a Fabric OneLake "
                            "row constraint. Translated, not blindly copied, because the two "
                            "platforms express these very differently.")
            rec.expand("row_filter")
            rec.caption("The actual predicate",
                        "The source UC filter expression behind the rule", 4.5,
                        say="And you can see the source filter expression behind every rule, "
                            "so nothing is a black box.")
            rec.clear_highlight()

        # ---------- Act 2b: column-level security ----------
        rec.caption("Column-level security",
                    "UC column masks become Fabric column constraints", 3.5,
                    say="Column level security works the same way.")
        if rec.expand("Column-level security"):
            rec.highlight('details:has(summary:has-text("Column-level security"))', 1.0)
            rec.caption("Column-level security (CLS)",
                        "Masked and hidden columns carried across platforms", 5,
                        say="Unity Catalog column masks become Fabric column constraints, so "
                            "masked and hidden columns stay enforced on both platforms.")
            rec.expand("column_mask")
            rec.caption("Mask function and target column",
                        "Full provenance for every masked column", 4.5,
                        say="You get full provenance. The masking function, the target column, "
                            "and the inputs it depends on.")
            rec.clear_highlight()

        # ---------- Act 2c: ABAC ----------
        rec.caption("Attribute-based access control",
                    "Tag-driven UC policies resolved to concrete Fabric constraints", 4,
                    say="Then there is attribute based access control.")
        if rec.highlight('span:text-is("ABAC")', 1.2):
            rec.caption("ABAC policies (tag-driven)",
                        "Governed-tag policies materialised into real permissions", 5.5,
                        say="Unity Catalog ABAC policies are tag driven and dynamic. The hub "
                            "resolves them against the governed tags and materialises them "
                            "into concrete Fabric constraints.")
            rec.clear_highlight()

        # ---------- Act 3: dry run ----------
        rec.caption("Dry run first", "Preview exactly what would change. Nothing is written.", 3.5,
                    say="Every apply starts as a dry run. Nothing is written until you say so.")
        rec.submit(direction_form.format("dbx_to_fabric"), PREVIEW_BUTTON)
        rec.scroll_to("#apply-result")
        rec.caption("Preview complete",
                    "Each row maps to a concrete Fabric OneLake role", 5,
                    say="The preview shows exactly which Fabric OneLake role each row will "
                        "become, before anything changes.")

        # ---------- Act 4: apply ----------
        if live:
            rec.goto(f"/pairings/{pairing_id}")
            rec.scroll_to(direction_form.format("dbx_to_fabric"))
            rec.caption("Applying to Fabric",
                        "Creating OneLake data access roles from Unity Catalog grants", 3.5,
                        say="Now let's apply it for real, creating OneLake data access roles "
                            "from the Unity Catalog grants.")
            rec.submit(direction_form.format("dbx_to_fabric"), SYNC_BUTTON)
            rec.scroll_to("#apply-result")
            rec.caption("Unity Catalog -> Fabric complete",
                        "Fabric now mirrors Databricks permissions", 5,
                        say="Done. Fabric now mirrors the Databricks permissions, including "
                            "the row and column level rules.")

            rec.goto("/")
            rec.caption("Alignment restored", "The estate is back in sync", 5,
                        say="And the dashboard shows the estate back in alignment.")

            # ---------- Act 5: reverse direction ----------
            rec.caption("Now the other direction",
                        "Clearing Unity Catalog grants so Fabric becomes the source", 4,
                        say="But this works both ways. Let's clear the Unity Catalog side, so "
                            "that Fabric becomes the source of truth.")
            _clear_side(pairing_id, "dbx")

            rec.goto(f"/pairings/{pairing_id}?refresh=1")
            rec.scroll_to(direction_form.format("fabric_to_dbx"))
            rec.caption("Fabric -> Unity Catalog",
                        "Now Fabric holds permissions that Unity Catalog is missing", 5,
                        say="Now Fabric holds the permissions, and Unity Catalog is the one "
                            "that's missing them.")
            rec.submit(direction_form.format("fabric_to_dbx"), SYNC_BUTTON)
            rec.scroll_to("#apply-result")
            rec.caption("Fabric -> Unity Catalog complete",
                        "Permissions written back into Databricks", 5,
                        say="And they are written straight back into Unity Catalog. Fully "
                            "bidirectional, with exactly the same safety model.")

        # ---------- Close ----------
        rec.goto("/operations")
        rec.caption("Every change is audited",
                    "Full audit trail, drift history and rollback plans", 5,
                    say="And every single change is audited. A full trail, drift history over "
                        "time, and a rollback plan for any apply.")
        rec.goto("/")
        rec.caption("Bidirectional. Dry-run by default. Fully audited.",
                    "Fabric Unified Permission Hub", 5,
                    say="Bidirectional. Dry run by default. Fully audited. That's the Fabric "
                        "Unified Permission Hub.")

        page.wait_for_timeout(1200)
        timeline = list(rec.timeline)
        context.close()
        browser.close()

    videos = sorted(VIDEO_DIR.glob("*.webm"), key=lambda p: p.stat().st_mtime)
    if videos:
        manifest = VIDEO_DIR / "timeline.json"
        manifest.write_text(
            json.dumps({"video": videos[-1].name, "cues": timeline}, indent=2),
            encoding="utf-8",
        )
        print(f"\nrecorded: {videos[-1].resolve()}")
        print(f"size: {videos[-1].stat().st_size / 1_000_000:.1f} MB")
        print(f"timeline: {manifest.resolve()} ({len(timeline)} cues)")
    return 0


def main() -> int:
    args = sys.argv[1:]
    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        print(__doc__)
        return 2
    base = "http://127.0.0.1:8011"
    for token in args:
        if token.startswith("--base="):
            base = token.split("=", 1)[1]
    return run(positional[0], base, "--live" in args)


if __name__ == "__main__":
    raise SystemExit(main())
