"""Prepare short wooden chess sounds from Kenney's CC0 Impact Sounds 1.0.

Original OGGs, decoded PCM sources and licence are in assets/audio.
Rebuilding/checking the runtime WAVs needs only Python's standard library.
"""

import argparse
import hashlib
import io
from pathlib import Path
import struct
import wave


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets/audio/kenney-impact-sounds"
SAMPLE_RATE = 44100
SAMPLES = {
    False: ("impactWood_light_000.wav", .12, .65,
            "97b4548ef9b6579ccd882a2f409920b00cf6498a461e25eb0533074be99e82a6"),
    True: ("impactWood_medium_002.wav", .22, .72,
           "43f6c8febc8e34b79e80fb232b9ee93ab5105c77ad409322ba7276356fa04340"),
}


def make_wav(capture=False):
    filename, duration, amplitude, expected_hash = SAMPLES[bool(capture)]
    data = (SOURCE / filename).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError(f"Unexpected source audio: {filename}")
    with wave.open(io.BytesIO(data), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getframerate() != SAMPLE_RATE:
            raise ValueError("Source must be signed PCM16 at 44100 Hz")
        channels = wav.getnchannels()
        raw = wav.readframes(round(SAMPLE_RATE * duration))
    values = struct.unpack(f"<{len(raw) // 2}h", raw)
    samples = [sum(values[i:i + channels]) / channels
               for i in range(0, len(values), channels)]
    # Keep the wood transient and body; soften sample boundaries to avoid clicks.
    for i in range(len(samples)):
        samples[i] *= min(1.0, i / (SAMPLE_RATE * .001),
                          (len(samples) - 1 - i) / (SAMPLE_RATE * .008))
    peak = max(map(abs, samples))
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
                    raise SystemExit(f"Audio differs from prepared sample: {path}")
            else:
                folder.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            print(f"{'Verified' if args.check else 'Prepared'} {path.relative_to(ROOT)} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
