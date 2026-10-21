"""Generate the end-to-end test video: 640x360, 30 fps, a big frame counter, keyframe every
second, VP8/WebM (plays in Playwright's Chromium). Output: demo/e2e/test_video.webm.

The frame counter is what the end-to-end test reads (via requestVideoFrameCallback's
mediaTime: frame = round(mediaTime * 30)); it's also drawn so you can check by eye.
"""
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

OUT = Path(__file__).resolve().parent / "test_video.webm"
SECONDS = 480
FPS = 30


def main(seconds=SECONDS):
    font = "C\\:/Windows/Fonts/arial.ttf"
    text = (f"drawtext=fontfile='{font}':text='frame %{{n}}':fontsize=64:fontcolor=white:x=40:y=120,"
            f"drawtext=fontfile='{font}':text='t %{{pts\\:hms}}':fontsize=40:fontcolor=yellow:x=40:y=220")
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-f", "lavfi", "-i", f"color=c=0x203040:s=640x360:r={FPS}:d={seconds}",
           "-vf", text, "-c:v", "libvpx", "-b:v", "400k", "-g", str(FPS), "-keyint_min", str(FPS),
           "-deadline", "realtime", "-cpu-used", "8", "-an", str(OUT)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB, {seconds} s)")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else SECONDS)
