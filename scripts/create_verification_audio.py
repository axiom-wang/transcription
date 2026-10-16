"""Generate test media from macOS say output, using installed PyAV only."""

from pathlib import Path

import av

ROOT = Path(__file__).resolve().parents[1]
source = ROOT / "verification" / "sample.aiff"
for extension in ("m4a", "mp3", "wav", "mov", "mp4"):
    destination = ROOT / "verification" / "inbox" / f"中文与English测试.{extension}"
    codec, fmt = (
        ("pcm_s16le", "s16")
        if extension == "wav"
        else (("libmp3lame", "fltp") if extension == "mp3" else ("aac", "fltp"))
    )
    with av.open(str(source)) as incoming, av.open(str(destination), "w") as outgoing:
        stream = outgoing.add_stream(codec, rate=16000)
        stream.layout = "mono"
        resampler = av.AudioResampler(format=fmt, layout="mono", rate=16000)
        for frame in incoming.decode(audio=0):
            for converted in resampler.resample(frame):
                for packet in stream.encode(converted):
                    outgoing.mux(packet)
        for converted in resampler.resample(None):
            for packet in stream.encode(converted):
                outgoing.mux(packet)
        for packet in stream.encode(None):
            outgoing.mux(packet)
    print(destination)
config = (ROOT / "config.toml").read_text().replace("enabled = true", "enabled = false")
config = config.replace('models = "models"', 'models = "../models"')
config = config.replace("settle_seconds = 30", "settle_seconds = 0")
(ROOT / "verification" / "config.toml").write_text(config)
