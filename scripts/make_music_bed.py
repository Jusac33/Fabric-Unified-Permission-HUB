"""Synthesise an original, energetic music bed for the demo video.

Generates the audio from scratch with the standard library, so the result is
original and carries no licensing risk - important for a public post, where a
copyrighted track can get the audio muted or the video taken down.

Style: driving 124 BPM electronic. Four-on-the-floor kick, offbeat hats, a
plucked sixteenth-note arpeggio and a moving bassline over an Am-F-C-G loop,
with the arrangement opening up over the first few bars so the intro narration
is not fighting a full mix.

The drive deliberately lives in the low end (kick, bass) and the top (hats),
with the arpeggio kept modest, because the muxer sidechain-ducks this bed under
the voiceover and anything dense in the speech band survives ducking badly.

Usage:
    python scripts/make_music_bed.py [--seconds=210] [--out=recordings/music_bed.wav]
"""
from __future__ import annotations

import array
import math
import random
import sys
import wave
from pathlib import Path

SAMPLE_RATE = 48000
BPM = 124.0
BEAT = 60.0 / BPM
BAR = BEAT * 4.0

# Am - F - C - G, as (bass root, arpeggio/stab chord tones).
PROGRESSION = [
    (110.00, (220.00, 261.63, 329.63)),   # Am
    (87.31,  (174.61, 220.00, 261.63)),   # F
    (130.81, (261.63, 329.63, 392.00)),   # C
    (98.00,  (196.00, 246.94, 293.66)),   # G
]
BARS_PER_CHORD = 2

# The progression repeats, so only a few dozen distinct notes are ever needed.
# Rendering each one once keeps this in seconds rather than minutes.
_NOTE_CACHE: dict = {}


def _mix(buffer: list[float], start: int, samples: list[float]) -> None:
    total = len(buffer)
    if start >= total:
        return
    for offset, value in enumerate(samples):
        index = start + offset
        if index >= total:
            return
        buffer[index] += value


def _kick(gain: float = 1.05) -> list[float]:
    """Punchy four-on-the-floor kick: fast pitch sweep into a short body."""
    length = int(0.24 * SAMPLE_RATE)
    out = [0.0] * length
    phase = 0.0
    for i in range(length):
        t = i / SAMPLE_RATE
        frequency = 46.0 + 95.0 * math.exp(-t * 30.0)
        phase += 2.0 * math.pi * frequency / SAMPLE_RATE
        out[i] = gain * math.exp(-t * 6.5) * math.sin(phase)
    return out


def _hat(seed: int, gain: float = 0.30) -> list[float]:
    """Bright noise burst; differentiated to push its energy well above speech."""
    rng = random.Random(seed)
    length = int(0.05 * SAMPLE_RATE)
    out = [0.0] * length
    previous = 0.0
    for i in range(length):
        t = i / SAMPLE_RATE
        noise = rng.uniform(-1.0, 1.0)
        out[i] = gain * (noise - previous) * math.exp(-t * 95.0)
        previous = noise
    return out


def _bass(frequency: float, seconds: float, gain: float = 0.52) -> list[float]:
    length = int(seconds * SAMPLE_RATE)
    out = [0.0] * length
    # One-pole lowpass keeps the bass round instead of buzzy.
    dt = 1.0 / SAMPLE_RATE
    rc = 1.0 / (2.0 * math.pi * 260.0)
    alpha = dt / (rc + dt)
    state = 0.0
    for i in range(length):
        t = i / SAMPLE_RATE
        phase = 2.0 * math.pi * frequency * t
        raw = math.sin(phase) + 0.5 * math.sin(2 * phase) + 0.22 * math.sin(3 * phase)
        state += alpha * (raw - state)
        attack = min(1.0, t * 500.0)
        out[i] = gain * attack * math.exp(-t * 4.6) * state
    return out


def _pluck(frequency: float, seconds: float, gain: float = 0.17) -> list[float]:
    length = int(seconds * SAMPLE_RATE)
    out = [0.0] * length
    for i in range(length):
        t = i / SAMPLE_RATE
        phase = 2.0 * math.pi * frequency * t
        raw = math.sin(phase) + 0.32 * math.sin(2 * phase) + 0.14 * math.sin(3 * phase)
        out[i] = gain * math.exp(-t * 12.0) * raw
    return out


def _stab(tones: tuple, seconds: float, gain: float = 0.13) -> list[float]:
    length = int(seconds * SAMPLE_RATE)
    out = [0.0] * length
    for i in range(length):
        t = i / SAMPLE_RATE
        envelope = math.exp(-t * 3.4) * min(1.0, t * 60.0)
        total = 0.0
        for tone in tones:
            total += math.sin(2.0 * math.pi * tone * t)
        out[i] = gain * envelope * total / len(tones)
    return out


def _cached(kind: str, frequency: float, seconds: float, gain: float) -> list[float]:
    key = (kind, round(frequency, 2), round(seconds, 4), gain)
    cached = _NOTE_CACHE.get(key)
    if cached is None:
        if kind == "bass":
            cached = _bass(frequency, seconds, gain)
        else:
            cached = _pluck(frequency, seconds, gain)
        _NOTE_CACHE[key] = cached
    return cached


def _render(seconds: float) -> list[float]:
    total = int(seconds * SAMPLE_RATE)
    buffer = [0.0] * total

    kick = _kick()
    hats = [_hat(seed) for seed in range(4)]
    stabs: dict = {}

    step_seconds = BAR / 16.0
    bar = 0
    while bar * BAR < seconds:
        bar_start = bar * BAR
        root, tones = PROGRESSION[(bar // BARS_PER_CHORD) % len(PROGRESSION)]

        # Arrangement: the kit lands from the first bar so the video opens with
        # energy - the first seconds carry no narration and are what stop a
        # scroll - then the hats add a lift once it is under way.
        drums = True
        full = bar >= 1

        for beat in range(4):
            beat_at = bar_start + beat * BEAT
            sample_at = int(beat_at * SAMPLE_RATE)
            if drums:
                _mix(buffer, sample_at, kick)
                _mix(buffer, sample_at, _cached("bass", root, BEAT * 0.9, 0.52))
                # Octave lift on the second half of beat 3 keeps the line moving.
                lift = root * 2.0 if beat == 2 else root
                _mix(buffer, int((beat_at + BEAT * 0.5) * SAMPLE_RATE),
                     _cached("bass", lift, BEAT * 0.45, 0.3))
            if full:
                _mix(buffer, int((beat_at + BEAT * 0.5) * SAMPLE_RATE),
                     hats[beat % len(hats)])

        stab = stabs.get(tones)
        if stab is None:
            stab = _stab(tones, BEAT * 1.7)
            stabs[tones] = stab
        for beat in (0, 2):
            _mix(buffer, int((bar_start + beat * BEAT) * SAMPLE_RATE), stab)

        for step in range(16):
            tone = tones[step % len(tones)]
            octave = 2.0 if (step // len(tones)) % 2 else 1.0
            _mix(buffer, int((bar_start + step * step_seconds) * SAMPLE_RATE),
                 _cached("pluck", tone * octave, step_seconds * 1.9, 0.17))

        bar += 1
    return buffer


def _soft_clip(buffer: list[float]) -> None:
    """Tame the peaks where kick and bass land together, without pumping."""
    for i, sample in enumerate(buffer):
        buffer[i] = math.tanh(sample * 1.15)


def _normalise(buffer: list[float], peak: float = 0.72) -> None:
    highest = max((abs(s) for s in buffer), default=0.0)
    if highest <= 0:
        return
    scale = peak / highest
    for i in range(len(buffer)):
        buffer[i] *= scale


def main() -> int:
    seconds = 210.0
    out = Path("recordings/music_bed.wav")
    for token in sys.argv[1:]:
        if token.startswith("--seconds="):
            seconds = float(token.split("=", 1)[1])
        elif token.startswith("--out="):
            out = Path(token.split("=", 1)[1])

    print(f"rendering {seconds:.0f}s energetic bed at {BPM:.0f} BPM...")
    buffer = _render(seconds)
    _soft_clip(buffer)
    _normalise(buffer)

    # Very short fades: long enough to avoid a click, short enough that the
    # track is at full energy almost immediately.
    fade = int(0.25 * SAMPLE_RATE)
    for i in range(min(fade, len(buffer))):
        buffer[i] *= i / fade
        buffer[-(i + 1)] *= i / fade

    frames = array.array("h")
    for sample in buffer:
        value = int(max(-1.0, min(1.0, sample)) * 32767)
        frames.append(value)
        frames.append(value)

    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(frames.tobytes())

    print(f"wrote {out.resolve()}  ({out.stat().st_size / 1_000_000:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
