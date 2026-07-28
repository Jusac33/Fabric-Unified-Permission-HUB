"""Generate a neural voiceover from a recording's timeline and mux it into MP4.

Reads ``recordings/timeline.json`` produced by ``scripts/record_demo.py``,
synthesises one audio clip per caption cue with Edge neural TTS, delays each
clip to its cue timestamp so narration stays in sync with the picture, mixes
them into a single track, and muxes that with the captured video.

Outputs H.264/AAC MP4, which is what LinkedIn accepts (the raw Playwright
capture is silent WebM).

Usage:
    python scripts/add_voiceover.py [--voice=en-US-AndrewNeural]
                                    [--rate=+0%] [--out=recordings/demo.mp4]
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import edge_tts
import imageio_ffmpeg

RECORDINGS = Path("recordings")
VOICE_DIR = RECORDINGS / "voice"
DURATION_CACHE = RECORDINGS / "narration_durations.json"
DEFAULT_VOICE = "en-US-AndrewNeural"
# Minimum silence between two spoken lines.
MIN_PAUSE = 0.45


def narration_key(text: str) -> str:
    return hashlib.sha1(" ".join((text or "").split()).encode("utf-8")).hexdigest()[:16]


async def _synthesize(cues: list[dict], voice: str, rate: str) -> list[Path]:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, cue in enumerate(cues):
        text = (cue.get("say") or "").strip()
        out = VOICE_DIR / f"cue_{index:03d}.mp3"
        if not text:
            paths.append(out)
            continue
        await edge_tts.Communicate(text, voice, rate=rate).save(str(out))
        paths.append(out)
        print(f"  [{index:02d}] {cue['t']:7.2f}s  {text[:64]}...")
    return paths


def _probe_duration(ffmpeg: str, path: Path) -> float:
    result = subprocess.run(
        [ffmpeg, "-i", str(path)], capture_output=True, text=True
    )
    for line in result.stderr.splitlines():
        if "Duration:" in line:
            stamp = line.split("Duration:")[1].split(",")[0].strip()
            hours, minutes, seconds = stamp.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return 0.0


def _schedule_without_overlap(times: list[float], durations: list[float]) -> list[float]:
    """Push each cue later if the previous line is still speaking.

    Caption hold times are authored by hand and can be shorter than the
    synthesised speech, which would otherwise make two lines talk over each
    other. Audio legibility wins over exact cue timing.
    """
    placed: list[float] = []
    previous_end = 0.0
    for start, spoken in zip(times, durations):
        adjusted = max(start, previous_end + MIN_PAUSE) if placed else start
        placed.append(round(adjusted, 3))
        previous_end = adjusted + spoken
    return placed


def _align_cues_to_video(cues: list[dict], video_len: float) -> float:
    """Rescale wall-clock cue times onto the video's own clock.

    record_demo.py timestamps cues with time.time(), but Playwright encodes at a
    variable frame rate and drops frames while the page is busy - a real apply
    pegs the browser for over a minute. The capture therefore ends up shorter
    than the wall time it covers, and the gap grows as the run proceeds, so
    captions and narration slide progressively later than the footage they
    describe. A single linear factor removes almost all of it.
    """
    if not cues or video_len <= 0:
        return 1.0
    last = cues[-1]
    wall_total = float(last["t"]) + float(last.get("hold") or 0.0) + 1.2
    if wall_total <= 0:
        return 1.0
    scale = video_len / wall_total
    # Only correct a meaningful drift; tiny differences are encoder rounding.
    if abs(1.0 - scale) < 0.005:
        return 1.0
    for cue in cues:
        cue["t"] = round(float(cue["t"]) * scale, 3)
        if cue.get("hold"):
            cue["hold"] = round(float(cue["hold"]) * scale, 3)
    return scale


def _plan_tighten(cues: list[dict], durations: list[float], video_len: float,
                  max_gap: float) -> tuple[list[tuple[float, float]], list[float]]:
    """Cut dead air down to ``max_gap`` seconds between spoken cues.

    Returns the source segments to keep and each cue's new timestamp in the
    tightened output. Long waits (a real apply can idle for a minute) otherwise
    dominate the running time.

    Each cue's window is taken from the caption ``hold`` recorded by
    record_demo.py, not from the narration length. The recorder already holds a
    caption for at least as long as its line takes to speak, so ``hold`` is the
    authoritative on-screen window; using the shorter narration length made
    windows overlap and pushed later cues onto footage that had already moved on.
    """
    windows: list[list[float]] = []
    for cue, spoken in zip(cues, durations):
        start = float(cue["t"])
        hold = float(cue.get("hold") or (spoken + SPEECH_PAD))
        end = min(start + max(hold, spoken), video_len)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])

    segments: list[list[float]] = []
    cursor = 0.0
    for start, end in windows:
        seg_start = max(cursor, start - max_gap) if (start - cursor) > max_gap else cursor
        if end <= seg_start:
            continue
        if segments and seg_start <= segments[-1][1] + 0.01:
            segments[-1][1] = max(segments[-1][1], end)
        else:
            segments.append([seg_start, end])
        cursor = end
    tail_end = min(cursor + max_gap, video_len)
    if segments and tail_end > cursor:
        segments[-1][1] = tail_end

    merged = [(s, e) for s, e in segments if e > s]

    new_times: list[float] = []
    for cue in cues:
        t = float(cue["t"])
        elapsed = 0.0
        mapped = 0.0
        for start, end in merged:
            if t >= end:
                elapsed += end - start
                mapped = elapsed
            elif t >= start:
                mapped = elapsed + (t - start)
                break
            else:
                mapped = elapsed
                break
        new_times.append(round(mapped, 2))
    return merged, new_times


def main() -> int:
    args = sys.argv[1:]
    voice, rate = DEFAULT_VOICE, "+0%"
    out_path = RECORDINGS / "demo.mp4"
    max_gap = 0.0
    music_path: Path | None = None
    # The energetic bed is a dense mix (~-11.5 LUFS on its own). At this gain the
    # music-only passages land near -23 dB, which reads as present rather than
    # background, while sidechain ducking keeps it off the narration - measured,
    # speech windows shift only ~0.4 dB between -16 dB and -12 dB here.
    music_gain = -12.0  # dB, before ducking
    for token in args:
        if token.startswith("--voice="):
            voice = token.split("=", 1)[1]
        elif token.startswith("--rate="):
            rate = token.split("=", 1)[1]
        elif token.startswith("--out="):
            out_path = Path(token.split("=", 1)[1])
        elif token.startswith("--tighten"):
            max_gap = float(token.split("=", 1)[1]) if "=" in token else 5.0
        elif token.startswith("--music="):
            music_path = Path(token.split("=", 1)[1])
        elif token.startswith("--music-gain="):
            music_gain = float(token.split("=", 1)[1])

    manifest = RECORDINGS / "timeline.json"
    if not manifest.exists():
        print("no timeline.json - run scripts/record_demo.py first")
        return 1
    data = json.loads(manifest.read_text(encoding="utf-8"))
    video = RECORDINGS / data["video"]
    if not video.exists():
        candidates = sorted(RECORDINGS.glob("*.webm"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            print("no .webm capture found in recordings/")
            return 1
        video = candidates[-1]
    cues = [c for c in data.get("cues") or [] if (c.get("say") or "").strip()]
    if not cues:
        print("timeline has no spoken cues")
        return 1

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    video_len = _probe_duration(ffmpeg, video)
    scale = _align_cues_to_video(cues, video_len)
    print(f"video : {video.name}  ({video_len:.1f}s)")
    print(f"voice : {voice} (rate {rate})")
    print(f"cues  : {len(cues)}")
    if scale != 1.0:
        print(f"clock : rescaled cue times by {scale:.4f} "
              f"(capture is shorter than wall time; frames dropped under load)")
    clips = asyncio.run(_synthesize(cues, voice, rate))
    durations = [_probe_duration(ffmpeg, clip) for clip in clips]

    # Feed real speech lengths back so the recorder can hold each caption long
    # enough on the next take instead of guessing.
    DURATION_CACHE.write_text(
        json.dumps(
            {narration_key(c["say"]): round(d, 2) for c, d in zip(cues, durations)},
            indent=2,
        ),
        encoding="utf-8",
    )

    cue_times = [float(c["t"]) for c in cues]
    video_filter = None
    if max_gap > 0:
        segments, cue_times = _plan_tighten(cues, durations, video_len, max_gap)
        parts = []
        for index, (start, end) in enumerate(segments):
            parts.append(
                f"[0:v]trim=start={start:.3f}:end={end:.3f},"
                f"setpts=PTS-STARTPTS[tv{index}]"
            )
        concat_inputs = "".join(f"[tv{i}]" for i in range(len(segments)))
        parts.append(f"{concat_inputs}concat=n={len(segments)}:v=1:a=0[vout]")
        video_filter = ";".join(parts)
        kept = sum(e - s for s, e in segments)
        print(f"\ntighten: {video_len:.0f}s -> {kept:.0f}s "
              f"({len(segments)} segments, max gap {max_gap:g}s)")

    adjusted = _schedule_without_overlap(cue_times, durations)
    shifted = sum(1 for a, b in zip(adjusted, cue_times) if a - b > 0.05)
    if shifted:
        print(f"overlap guard: {shifted} cue(s) delayed to avoid two voices at once")
    cue_times = adjusted

    # Publish the final cue positions so a post-production pass can place
    # feed-legible caption strips at exactly the right moments.
    (RECORDINGS / "timeline-final.json").write_text(
        json.dumps({
            "video": out_path.name,
            "cues": [
                {
                    "start": round(start, 2),
                    "duration": round(spoken, 2),
                    "title": cue.get("title", ""),
                    "subtitle": cue.get("subtitle", ""),
                }
                for cue, start, spoken in zip(cues, cue_times, durations)
            ],
        }, indent=2),
        encoding="utf-8",
    )

    # Build: delay each clip to its cue time, then mix into one track.
    command = [ffmpeg, "-y", "-i", str(video)]
    for clip in clips:
        command += ["-i", str(clip)]
    music_index = None
    if music_path and music_path.exists():
        music_index = 1 + len(clips)
        command += ["-i", str(music_path)]
    elif music_path:
        print(f"music file not found, continuing without it: {music_path}")

    filters = []
    if video_filter:
        filters.append(video_filter)
    labels = []
    for index, start in enumerate(cue_times):
        delay_ms = max(0, int(float(start) * 1000))
        stream = index + 1  # input 0 is the video
        filters.append(f"[{stream}:a]adelay={delay_ms}|{delay_ms}[d{index}]")
        labels.append(f"[d{index}]")
    # normalize=0 keeps each clip at full volume instead of dividing by input count.
    # The TTS clips are 24 kHz mono, which some players and hardware decoders will
    # not render at all, so the mix is resampled to 48 kHz stereo and loudness
    # normalised to roughly broadcast level before encoding.
    voice_chain = (
        "".join(labels)
        + f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0,"
        + "aresample=48000,aformat=channel_layouts=stereo,"
        + "loudnorm=I=-16:TP=-1.5:LRA=11"
    )

    if music_index is None:
        filters.append(voice_chain + "[vomix]")
    else:
        filters.append(voice_chain + "[speech]")
        # One copy is heard, the other keys the compressor.
        filters.append("[speech]asplit=2[speech_out][keyed]")
        # Loop so a short bed still covers a long video, then duck it under the
        # narration rather than relying on a fixed low level.
        filters.append(
            f"[{music_index}:a]aloop=loop=-1:size=2e9,aresample=48000,"
            f"aformat=channel_layouts=stereo,volume={music_gain}dB[bed]"
        )
        filters.append(
            "[bed][keyed]sidechaincompress=threshold=0.03:ratio=8:"
            "attack=25:release=450[ducked]"
        )
        filters.append(
            "[speech_out][ducked]amix=inputs=2:normalize=0:"
            "dropout_transition=0,alimiter=limit=0.95[vomix]"
        )
        print(f"music : {music_path.name} at {music_gain:g} dB, ducked under narration")

    # Without padding, -shortest clips the video the instant the last word ends.
    filters.append("[vomix]apad=pad_dur=4[vo]")

    command += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]" if video_filter else "0:v:0", "-map", "[vo]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        "-shortest",
        str(out_path),
    ]

    print("\nmuxing...")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print("ffmpeg failed:")
        print("\n".join(result.stderr.strip().splitlines()[-15:]))
        return 1

    print(f"\nwrote {out_path.resolve()}")
    print(f"  size    : {out_path.stat().st_size / 1_000_000:.1f} MB")
    print(f"  duration: {_probe_duration(ffmpeg, out_path):.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
