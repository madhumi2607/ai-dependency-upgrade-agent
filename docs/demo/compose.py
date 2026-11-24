"""Assemble the captions-only demo from its parts and record when each scene starts.

    title card   (rendered from record_demo.TITLE)
    live run     demo-run.mp4, from its "runs" scene up to its own end card (record_demo.py)
    multi-agent  multi-segment.mp4, a walkthrough of a recorded reviewer run (record_multi.py)
    end card     (rendered from record_demo.END)

Writes demo-silent.mp4 and scenes.json; narrate.py then adds the voiceover as demo.mp4.

    uv run --with playwright==1.48.0 --with imageio-ffmpeg python docs/demo/compose.py
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent
sys.path.insert(0, str(OUT))
from record_demo import CARD, END, SIZE, TITLE  # noqa: E402

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
END_SECONDS = 16.0  # long enough to read the results card


def duration(path: Path) -> float:
    info = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def render(body: str, path: Path) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=SIZE, color_scheme="dark")
        page.set_content(CARD.format(body=body))
        page.wait_for_timeout(300)
        page.screenshot(path=str(path))
        browser.close()


def main() -> None:
    run = {s["scene"]: s["at"] for s in json.loads((OUT / "scenes-run.json").read_text(encoding="utf-8"))["scenes"]}
    multi = json.loads((OUT / "multi-scenes.json").read_text(encoding="utf-8"))["scenes"]
    title_len, run_end = run["runs"], run["end"]  # the run is cut at its own (old) title and end cards
    multi_len = duration(OUT / "multi-segment.mp4")

    with tempfile.TemporaryDirectory() as tmp:
        title_png, end_png = Path(tmp) / "title.png", Path(tmp) / "end.png"
        render(TITLE, title_png)
        render(END, end_png)
        norm = "fps=25,format=yuv420p,setsar=1"
        graph = ";".join([
            f"[0:v]{norm}[a]",
            f"[1:v]trim=start={title_len}:end={run_end},setpts=PTS-STARTPTS,{norm}[b]",
            f"[2:v]{norm}[c]",
            f"[3:v]{norm}[d]",
            "[a][b][c][d]concat=n=4:v=1:a=0[v]",
        ])
        subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                        "-loop", "1", "-t", f"{title_len}", "-i", str(title_png),
                        "-i", str(OUT / "demo-run.mp4"),
                        "-i", str(OUT / "multi-segment.mp4"),
                        "-loop", "1", "-t", f"{END_SECONDS}", "-i", str(end_png),
                        "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-crf", "26", "-preset", "slow", "-movflags", "+faststart", str(OUT / "demo-silent.mp4")],
                       check=True)

    scenes = [{"scene": name, "at": at} for name, at in run.items() if at < run_end]
    scenes += [{"scene": s["scene"], "at": round(run_end + s["at"], 2)} for s in multi]
    scenes.append({"scene": "end", "at": round(run_end + multi_len, 2)})
    (OUT / "scenes.json").write_text(json.dumps({"scenes": scenes}, indent=1), encoding="utf-8")
    print(f"demo-silent.mp4: {duration(OUT / 'demo-silent.mp4'):.1f}s; multi-agent part at {run_end:.1f}s, end at {scenes[-1]['at']:.1f}s")


if __name__ == "__main__":
    main()
