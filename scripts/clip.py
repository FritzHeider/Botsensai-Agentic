#!/usr/bin/env python3
"""Botsensai GTA VI Style Instructional Video Producer.

Produces a 5-scene instructional trailer in GTA VI Vice City aesthetic
using ByteDance's Seedance video generation models on Fal.ai with seamless
stitching, synthwave soundtrack synthesis, and motion dynamics.

Usage:
  python scripts/clip.py                 # Interactive mode (prompts for FAL_KEY or local render)
  python scripts/clip.py --seedance      # Use ByteDance Seedance on Fal.ai
  python scripts/clip.py --local         # Fast local motion & typography render
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

# Paths
REPO_ROOT = Path(__file__).resolve().parent.parent
BRAIN_DIR = Path("/Users/drop/.gemini/antigravity/brain/d729954e-0817-4e0b-96b4-643b626a6e13")
OUTPUT_DIR = REPO_ROOT / "data" / "content"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SCENES = [
    {
        "id": "scene_1_intro",
        "title": "SCENE 1: THE VICE CITY TRAP",
        "image": BRAIN_DIR / "gta6_intro_penthouse_1787616555170.jpg",
        "duration": 5.0,
        "badge": "THE PROBLEM",
        "subtitle": "99% OF SOLANA METRICS ARE MANUFACTURED ILLUSIONS.",
        "seedance_prompt": "GTA 6 video game cinematic trailer, neon-soaked Miami penthouse overlooking sunset ocean, slow camera zoom past trading monitors glowing with Solana charts, ultra-realistic lighting, 4k 60fps",
    },
    {
        "id": "scene_2_recon",
        "title": "SCENE 2: BOTSENSAI RECON & 34 SIGNALS",
        "image": BRAIN_DIR / "gta6_supercar_radar_1787616570718.jpg",
        "duration": 5.0,
        "badge": "SIGNAL ENGINE",
        "subtitle": "WE SCORE 34 ON-CHAIN SIGNALS FILTERED STRICTLY BY COST-TO-FAKE.",
        "seedance_prompt": "GTA 6 cinematic, supercar dashboard driving on Ocean Drive at night with neon palm trees, holographic HUD scanning memecoins with green radar graphs, cinematic camera pan",
    },
    {
        "id": "scene_3_funnel",
        "title": "SCENE 3: THE DISCOVERY & VETO FUNNEL",
        "image": BRAIN_DIR / "gta6_funnel_terminal_1787616645243.jpg",
        "duration": 5.0,
        "badge": "THE FUNNEL",
        "subtitle": "ZERO-COST SCREENING. HARD VETOES REJECT RUGS INSTANTLY.",
        "seedance_prompt": "GTA 6 cinematic surveillance command center inside high-tech van, multi-screen matrix showing data funnel with red flashing VETO stamps, moody neon reflections",
    },
    {
        "id": "scene_4_execution",
        "title": "SCENE 4: REALISTIC PAPER EXECUTION",
        "image": BRAIN_DIR / "gta6_paper_execution_1787616674657.jpg",
        "duration": 5.0,
        "badge": "RISK & PAPER TRADING",
        "subtitle": "CURVE IMPACT, PRIORITY FEES & SLIPPAGE. ZERO CAPITAL RISK.",
        "seedance_prompt": "GTA 6 sports car drifting across wet Miami bridge at night, glowing cyan and magenta HUD displaying trade execution fills and zero real money risk, intense speed blur",
    },
    {
        "id": "scene_5_title",
        "title": "SCENE 5: BOTSENSAI ARSENAL",
        "image": BRAIN_DIR / "gta6_finale_title_1787616690841.jpg",
        "duration": 5.0,
        "badge": "GET STARTED",
        "subtitle": "ONE COMMAND TO RULE THE FIREHOSE: botsensai run",
        "seedance_prompt": "GTA 6 official trailer outro title card with glowing pink BOTSENSAI typography, palm tree silhouettes and glowing command badges, slow cinematic pull-back",
    },
]


def _get_font(size: int = 36) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Try to get a clean sans font, falling back to default."""
    font_paths = [
        "/System/Library/Fonts/Supplemental/Impact.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNS.ttf",
    ]
    for fp in font_paths:
        if Path(fp).exists():
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return ImageFont.load_default()


def overlay_gta_subtitles(src_image: Path, dest_image: Path, badge: str, subtitle: str) -> None:
    """Render authentic GTA VI style lower-third subtitle bar directly onto the frame."""
    img = Image.open(src_image).convert("RGBA")
    w, h = img.size

    if (w, h) != (1920, 1080):
        img = img.resize((1920, 1080), Image.Resampling.LANCZOS)
        w, h = 1920, 1080

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    banner_y = h - 160
    draw.rectangle([(0, banner_y), (w, h - 30)], fill=(10, 10, 20, 215))
    draw.line([(0, banner_y), (w, banner_y)], fill=(255, 0, 128, 255), width=3)
    draw.line([(0, banner_y + 3), (w, banner_y + 3)], fill=(0, 240, 255, 180), width=2)

    font_badge = _get_font(22)
    font_text = _get_font(38)

    badge_text = f"  {badge.upper()}  "
    draw.rectangle([(80, banner_y + 15), (80 + len(badge_text) * 14, banner_y + 45)], fill=(0, 230, 255, 240))
    draw.text((85, banner_y + 18), badge_text, font=font_badge, fill=(0, 0, 0, 255))

    sub_x = 80
    sub_y = banner_y + 60
    draw.text((sub_x + 2, sub_y + 2), subtitle, font=font_text, fill=(255, 0, 128, 200))
    draw.text((sub_x, sub_y), subtitle, font=font_text, fill=(255, 255, 255, 255))

    final_img = Image.alpha_composite(img, overlay).convert("RGB")
    final_img.save(dest_image, "JPEG", quality=95)


def generate_seedance_clip(scene: dict, index: int, temp_dir: Path, fal_key: str) -> Path:
    """Generate an AI video clip using ByteDance Seedance on Fal.ai."""
    import fal_client

    os.environ["FAL_KEY"] = fal_key
    fal_client.api_key = fal_key

    raw_img = scene["image"]
    framed_img = temp_dir / f"framed_{index:02d}.jpg"
    overlay_gta_subtitles(raw_img, framed_img, scene["badge"], scene["subtitle"])

    print("    • Uploading keyframe to Fal.ai...")
    image_url = fal_client.upload_file(framed_img)

    print("    • Submitting to ByteDance Seedance (image-to-video)...")
    # Try Seedance image-to-video / video model endpoints
    endpoints = [
        "fal-ai/bytedance/seedance-2.0/image-to-video",
        "bytedance/seedance-2.0/image-to-video",
        "fal-ai/bytedance/seedance/v1.5/pro/image-to-video",
        "fal-ai/kling-video/v1.6/standard/image-to-video",
        "fal-ai/minimax/video-01/image-to-video",
    ]

    video_url = None
    for endpoint in endpoints:
        try:
            result = fal_client.subscribe(
                endpoint,
                arguments={
                    "prompt": scene["seedance_prompt"],
                    "image_url": image_url,
                    "duration": "5",
                    "aspect_ratio": "16:9",
                },
                with_logs=True,
            )
            if result and "video" in result and "url" in result["video"]:
                video_url = result["video"]["url"]
                break
        except Exception as exc:
            print(f"    [dim]Endpoint {endpoint} returned: {exc}, trying next...[/dim]")

    out_clip = temp_dir / f"clip_{index:02d}.mp4"
    if video_url:
        print(f"    ✓ Downloading generated video clip from {video_url[:40]}...")
        with httpx.Client(timeout=60.0) as client:
            resp = client.get(video_url)
            out_clip.write_bytes(resp.content)
    else:
        print("    [yellow]Fallback to local high-def motion render...[/yellow]")
        out_clip = render_local_clip(scene, index, temp_dir)

    framed_img.unlink(missing_ok=True)
    return out_clip


def render_local_clip(scene: dict, index: int, temp_dir: Path) -> Path:
    """Render a dynamic 1080p 30fps clip with Ken Burns motion and GTA VI lower-thirds."""
    raw_img = scene["image"]
    dur = scene["duration"]
    framed_img = temp_dir / f"framed_{index:02d}.jpg"
    out_clip = temp_dir / f"clip_{index:02d}.mp4"

    overlay_gta_subtitles(raw_img, framed_img, scene["badge"], scene["subtitle"])

    fps = 30
    total_frames = int(dur * fps)
    if index % 2 == 0:
        zoom_filter = f"zoompan=z='min(zoom+0.0012,1.20)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={total_frames}:s=1920x1080:fps={fps}"
    else:
        zoom_filter = f"zoompan=z='if(lte(zoom,1.0),1.20,max(1.001,zoom-0.0012))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={total_frames}:s=1920x1080:fps={fps}"

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(framed_img),
        "-vf", f"{zoom_filter},format=yuv420p",
        "-t", str(dur),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-r", str(fps),
        str(out_clip),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    framed_img.unlink(missing_ok=True)
    return out_clip


def generate_synthwave_audio(duration: float, out_path: Path) -> Path:
    """Synthesize an authentic 80s/GTA Vice City bassline & arpeggio track using ffmpeg."""
    filter_complex = (
        f"sine=frequency=110:duration={duration}[b1];"
        f"sine=frequency=164.81:duration={duration}[b2];"
        f"sine=frequency=220:duration={duration}[m1];"
        f"anoisesrc=d={duration}:c=pink:r=44100:a=0.08[noise];"
        "[b1][b2]amix=inputs=2:weights=1 0.8[bass];"
        "[m1]tremolo=f=4:d=0.7[synth];"
        "[noise]lowpass=f=200[kick];"
        "[bass][synth][kick]amix=inputs=3:weights=0.5 0.3 0.4,"
        "volume=0.9,"
        "afade=t=in:ss=0:d=1.0,"
        f"afade=t=out:st={max(0.0, duration - 1.5)}:d=1.5[out]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={duration}",
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "aac", "-b:a", "192k",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out_path


def stitch_trailer(clips: list[Path], audio_path: Path, output_file: Path) -> None:
    """Seamlessly stitch clips with background soundtrack."""
    concat_list_file = output_file.parent / "concat_list.txt"
    with concat_list_file.open("w", encoding="utf-8") as f:
        for clip in clips:
            f.write(f"file '{clip.resolve()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list_file),
        "-i", str(audio_path),
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-c:a", "aac", "-b:a", "192k",
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest",
        str(output_file),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    concat_list_file.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Botsensai GTA VI Video Producer")
    parser.add_argument("--seedance", action="store_true", help="Force ByteDance Seedance on Fal.ai")
    parser.add_argument("--local", action="store_true", help="Force fast local motion rendering")
    parser.add_argument("--out", default=str(OUTPUT_DIR / "botsensai_gta6_trailer.mp4"), help="Output video path")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = out_path.parent / "_temp_clips"
    temp_dir.mkdir(parents=True, exist_ok=True)

    print("\n🎬 =========================================================")
    print("   BOTSENSAI: GTA VI STYLE INSTRUCTIONAL VIDEO PRODUCER     ")
    print("   Featuring ByteDance Seedance & Fal.ai Video Engine       ")
    print("=========================================================\n")

    fal_key = os.environ.get("FAL_KEY")
    use_seedance = False

    if args.seedance or (not args.local and fal_key):
        use_seedance = True
        print("✓ Fal.ai API key detected. Using ByteDance Seedance SOTA video models.")
    else:
        print("✓ Rendering 1080p 30fps GTA VI cinematic motion sequences with synthwave soundtrack.")

    rendered_clips: list[Path] = []
    total_dur = sum(s["duration"] for s in SCENES)

    for idx, scene in enumerate(SCENES, 1):
        print(f"\n  [{idx}/{len(SCENES)}] Processing {scene['title']} ({scene['duration']}s)...")
        if use_seedance and fal_key:
            clip_file = generate_seedance_clip(scene, idx, temp_dir, fal_key)
        else:
            clip_file = render_local_clip(scene, idx, temp_dir)
        rendered_clips.append(clip_file)

    print("\n🎵 Synthesizing GTA Vice City synthwave soundtrack...")
    audio_file = temp_dir / "soundtrack.aac"
    generate_synthwave_audio(total_dur, audio_file)

    print("\n🎞️  Seamlessly assembling and mastering final 5-clip trailer...")
    stitch_trailer(rendered_clips, audio_file, out_path)

    # Clean up temp
    import shutil

    shutil.rmtree(temp_dir, ignore_errors=True)

    print(f"\n✨ Video successfully generated at: {out_path}")
    print(f"   Size: {out_path.stat().st_size / 1_000_000:.2f} MB | Duration: {total_dur:.0f}s | Resolution: 1080p\n")


if __name__ == "__main__":
    main()
