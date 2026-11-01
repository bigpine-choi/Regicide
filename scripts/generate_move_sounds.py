"""Rebuild Regicide's original wooden move/capture sounds (no third-party samples).

Only Python's standard library is needed. Fixed seeds keep the PCM reproducible.
"""

import argparse
import io
import math
from pathlib import Path
import random
import struct
import wave


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 44100


def impact(t, rng, frequencies, decay):
    """An inharmonic wooden body plus a very short, softened contact transient."""
    attack = 1.0 - math.exp(-t / 0.0007)
    body = sum(weight * math.sin(2.0 * math.pi * frequency * t)
               * math.exp(-t / (decay / (1.0 + index * 0.55)))
               for index, (frequency, weight) in enumerate(frequencies))
    noise = rng.uniform(-1.0, 1.0) * 0.38 * math.exp(-t / 0.005)
    return attack * (body + noise)


def make_wav(capture=False):
    duration = 0.18 if capture else 0.12
    rng = random.Random(2803 if capture else 1409)
    frequencies = [(247, .50), (631, .56), (1087, .30), (1733, .12)] if capture else [
        (389, .45), (947, .50), (1621, .22), (2371, .09)]
    samples = []
    previous = 0.0
    for index in range(round(SAMPLE_RATE * duration)):
        t = index / SAMPLE_RATE
        value = impact(t, rng, frequencies, .025 if capture else .017)
        if capture and t >= .018:
            # A softer second contact makes taking a piece fuller, not louder.
            value += .36 * impact(t - .018, rng, frequencies, .017)
        previous = .35 * previous + .65 * value
        samples.append(previous * min(1.0, (duration - t) / .012))
    peak = max(map(abs, samples))
    amplitude = .72 if capture else .65
    pcm = b"".join(struct.pack("<h", round(value / peak * amplitude * 32767))
                   for value in samples)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify committed WAVs without writing")
    args = parser.parse_args()
    for filename, capture in [("move.wav", False), ("capture.wav", True)]:
        data = make_wav(capture)
        for folder in [ROOT / "godot/assets/audio", ROOT / "web/human-play/audio"]:
            path = folder / filename
            if args.check:
                if not path.exists() or path.read_bytes() != data:
                    raise SystemExit(f"Audio differs from generator: {path}")
            else:
                folder.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            print(f"{'Verified' if args.check else 'Generated'} {path.relative_to(ROOT)} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
