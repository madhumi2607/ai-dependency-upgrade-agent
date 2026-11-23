"""Add the voiceover to the recorded demo: demo-silent.mp4 + voice/*.mp3 -> demo.mp4.

Each clip in narration.json is tied to a scene from scenes.json (written by record_demo.py) and
starts at that scene (plus an optional offset) or right after the previous clip ends, whichever is
later, so clips never overlap. If the narration runs past the video, the last frame is held.
The narration describes the run recorded alongside it (agate-sql, 18 tests, 13 steps): if you
re-record, check those numbers against scenes.json and regenerate the affected clips.

    uv run --with imageio-ffmpeg python docs/demo/narrate.py
"""

import json
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg

HERE = Path(__file__).parent
GAP = 0.3  # seconds of silence between consecutive clips
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def duration(path: Path) -> float:
    info = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def schedule(clips: list[dict], scenes: dict[str, float]) -> list[tuple[Path, float, float]]:
    """(clip, start, end) for each clip, in order, with no overlaps."""
    out, free_at = [], 0.0
    for clip in clips:
        path = HERE / clip["file"]
        start = max(scenes[clip["scene"]] + clip.get("offset", 0), free_at)
        end = start + duration(path)
        out.append((path, start, end))
        free_at = end + GAP
    return out


def main() -> None:
    scenes = {s["scene"]: s["at"] for s in json.loads((HERE / "scenes.json").read_text(encoding="utf-8"))["scenes"]}
    clips = json.loads((HERE / "narration.json").read_text(encoding="utf-8"))["clips"]
    plan = schedule(clips, scenes)
    silent = HERE / "demo-silent.mp4"  # captions-only recording from record_demo.py
    video_len = duration(silent)
    hold = max(0.0, plan[-1][2] + 1.5 - video_len)  # let the last line finish, plus a beat

    inputs = ["-i", str(silent)]
    filters = []
    for i, (path, start, _) in enumerate(plan, 1):
        inputs += ["-i", str(path)]
        ms = int(start * 1000)
        filters.append(f"[{i}:a]adelay={ms}|{ms}[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(1, len(plan) + 1))
    filters.append(f"{mix}amix=inputs={len(plan)}:normalize=0[aout]")
    filters.append(f"[0:v]tpad=stop_mode=clone:stop_duration={hold:.2f}[vout]")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filters),
                    "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26",
                    "-preset", "slow", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                    str(HERE / "demo.mp4")], check=True)
    for path, start, end in plan:
        print(f"  {path.name}  {start:6.1f}s -> {end:6.1f}s")
    print(f"demo.mp4: {duration(HERE / 'demo.mp4'):.1f}s (video {video_len:.1f}s + {hold:.1f}s held), "
          f"{(HERE / 'demo.mp4').stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
