import os
import re
import sys
import json
import math
import time
import shutil
import subprocess

import requests
import imageio_ffmpeg
from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip, ColorClip

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
WORK_DIR = os.path.join(os.getcwd(), "_render_work")
os.makedirs(WORK_DIR, exist_ok=True)

RESOLUTION = (1920, 1080)
FPS = 30


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def notify_n8n_resume(payload):
    """
    POSTs to the n8n Wait node's resume-webhook URL so the paused n8n
    execution wakes back up and continues the loop. Called exactly once,
    at the very end of the run — on success OR failure — so a crashed
    render doesn't just leave n8n hanging until the Wait node times out.
    """
    resume_url = os.environ.get("N8N_RESUME_URL")
    if not resume_url:
        log("no N8N_RESUME_URL set — skipping resume callback (fine for local/manual runs)")
        return

    try:
        r = requests.post(resume_url, json=payload, timeout=30)
        log(f"resume webhook called -> status {r.status_code}")
        if r.status_code >= 300:
            log(f"WARNING: resume webhook returned non-2xx: {r.text[:300]}")
    except Exception as e:
        # Don't let a failed callback crash the job after rendering is done —
        # just log it loudly so it's visible in the Actions run log.
        log(f"WARNING: failed to call resume webhook: {e}")


def download_to(url, path):
    t0 = time.time()
    session = requests.Session()
    r = session.get(url, stream=True, timeout=60)
    content_type = r.headers.get("Content-Type", "")

    if "text/html" in content_type:
        text = r.text
        match = re.search(r'confirm=([0-9A-Za-z_\-]+)', text)
        token = match.group(1) if match else None
        if not token:
            for k, v in r.cookies.items():
                if k.startswith("download_warning"):
                    token = v
                    break
        if token:
            sep = "&" if "?" in url else "?"
            retry_url = f"{url}{sep}confirm={token}"
            r = session.get(retry_url, stream=True, timeout=60)
            content_type = r.headers.get("Content-Type", "")

    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)

    size_kb = os.path.getsize(path) / 1024
    log(f"downloaded {url[:60]}... -> {size_kb:.1f}KB, content-type={content_type}, took {time.time()-t0:.1f}s")

    if "text/html" in content_type or size_kb < 1:
        log("WARNING: downloaded file looks suspicious (html or tiny) — likely not the real file")

    return path


def render_single_scene(scene, batch_tmp):
    scene_index = int(scene["scene_index"])
    background_url = scene["background_url"]
    character_urls = scene.get("character_urls", [])
    audio_url = scene.get("audio_url")
    duration = scene.get("duration")

    if not audio_url and duration is None:
        raise ValueError(f"scene {scene_index}: must provide either 'audio_url' or 'duration'")

    scene_tmp = os.path.join(batch_tmp, f"_tmp_scene_{scene_index}")
    os.makedirs(scene_tmp, exist_ok=True)

    audio_clip = bg_clip = composite = None
    log(f"=== rendering scene {scene_index} ===")
    try:
        bg_path = download_to(background_url, os.path.join(scene_tmp, "bg.png"))

        if audio_url:
            audio_path = download_to(audio_url, os.path.join(scene_tmp, "audio.mp3"))
            audio_clip = AudioFileClip(audio_path)
            duration = audio_clip.duration
            log(f"loaded audio, duration={duration:.1f}s")
        else:
            duration = float(duration)
            log(f"rendering SILENT clip, duration={duration:.1f}s")

        bg_clip = ImageClip(bg_path).set_duration(duration).resize(RESOLUTION)

        zoom_in = (scene_index % 2 == 0)
        z_start, z_end = (1.0, 1.08) if zoom_in else (1.08, 1.0)
        pan_direction = 1 if (scene_index % 3 == 0) else -1

        bg_clip = bg_clip.resize(lambda t: z_start + (z_end - z_start) * (t / duration))
        max_pan_px = 30
        bg_clip = bg_clip.set_position(
            lambda t: (pan_direction * max_pan_px * (t / duration) - (max_pan_px / 2 if pan_direction > 0 else -max_pan_px / 2), 0)
        )

        darken_overlay = (ColorClip(size=RESOLUTION, color=(0, 0, 0))
                         .set_opacity(0.20)
                         .set_duration(duration))

        layers = [bg_clip, darken_overlay]
        num_chars = len(character_urls)
        width, height = RESOLUTION

        if num_chars <= 1:
            x_fracs = [0.5]
            char_height_frac = 0.88
        elif num_chars == 2:
            x_fracs = [0.30, 0.70]
            char_height_frac = 0.85
        else:
            x_fracs = [0.20 + 0.60 * (i / (num_chars - 1)) for i in range(num_chars)]
            char_height_frac = 0.78

        fade_dur = min(0.4, duration / 4)

        for i, char_url in enumerate(character_urls):
            char_path = download_to(char_url, os.path.join(scene_tmp, f"char_{i}.png"))
            target_h = int(height * char_height_frac)

            img_clip = ImageClip(char_path).set_duration(duration).resize(height=target_h)
            if img_clip.img.ndim == 3 and img_clip.img.shape[2] == 4:
                mask_clip = ImageClip(img_clip.img[:, :, 3] / 255.0, ismask=True).set_duration(duration).resize(height=target_h)
                img_clip = img_clip.set_mask(mask_clip)

            center_x = int(width * x_fracs[i])
            left_x = center_x - img_clip.w // 2
            img_clip = img_clip.set_position((left_x, height - target_h))

            img_clip = img_clip.crossfadein(fade_dur).crossfadeout(fade_dur)

            pulse_period = 3.0 + (i * 0.5)

            def make_pulse(period):
                return lambda t: 1.0 + 0.015 * math.sin(2 * math.pi * t / period)

            img_clip = img_clip.resize(make_pulse(pulse_period))

            layers.append(img_clip)

        log(f"placed {num_chars} characters with explicit alpha-masked crossfades")

        composite = CompositeVideoClip(layers, size=RESOLUTION).set_duration(duration)
        if audio_clip is not None:
            composite = composite.set_audio(audio_clip)

        out_path = os.path.join(batch_tmp, f"scene_{scene_index:04d}.mp4")

        composite.write_videofile(
            out_path, fps=FPS, codec="libx264", audio_codec="aac",
            preset="medium", threads=1,
            ffmpeg_params=["-pix_fmt", "yuv420p", "-crf", "18"], logger=None,
        )
        log(f"successfully rendered scene {scene_index} -> {out_path}")
        return out_path

    finally:
        for c in [audio_clip, bg_clip, composite]:
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        shutil.rmtree(scene_tmp, ignore_errors=True)


def concat_scenes(scene_paths, batch_tmp, part_name):
    list_path = os.path.join(batch_tmp, f"{part_name}_list.txt")
    with open(list_path, "w") as f:
        for p in scene_paths:
            f.write(f"file '{p}'\n")

    out_path = os.path.join(batch_tmp, f"{part_name}.mp4")
    cmd = [
        FFMPEG_EXE, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p", out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    os.remove(list_path)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr[-2000:]}")

    return out_path


def get_drive_service():
    creds = Credentials(
        None,
        refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
        client_id=os.environ["GDRIVE_CLIENT_ID"],
        client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("drive", "v3", credentials=creds)


def upload_to_drive(service, file_path, file_name, folder_id):
    file_metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaFileUpload(file_path, mimetype="video/mp4", resumable=True)
    uploaded = service.files().create(
        body=file_metadata, media_body=media, fields="id, name, webContentLink"
    ).execute()
    return uploaded


def main():
    scenes = json.loads(os.environ["SCENES_JSON"])

    output_folder_id = os.environ.get("OUTPUT_FOLDER_ID") or "1G9Gmc-VeAzy13bAO95xW9go7Z43R-HAA"
    default_part = f"part_{scenes[0]['scene_index']:04d}_{scenes[-1]['scene_index']:04d}"
    part_name = os.environ.get("PART_NAME") or default_part

    batch_tmp = os.path.join(WORK_DIR, part_name)
    os.makedirs(batch_tmp, exist_ok=True)

    # Everything from here down is wrapped so that no matter how it ends —
    # clean success, partial scene failures, or a hard crash — we ALWAYS
    # call the n8n resume webhook exactly once at the end. Otherwise a
    # failed run just leaves the n8n Wait node hanging until it times out.
    try:
        scene_paths = []
        errors = []
        for scene in scenes:
            try:
                path = render_single_scene(scene, batch_tmp)
                scene_paths.append(path)
            except Exception as e:
                log(f"ERROR rendering scene {scene.get('scene_index')}: {e}")
                errors.append({"scene_index": scene.get("scene_index"), "error": str(e)})

        if not scene_paths:
            log("FATAL: no scenes rendered successfully in this batch")
            notify_n8n_resume({
                "status": "error",
                "part_name": part_name,
                "error": "no scenes rendered successfully",
                "failed_scenes": errors,
            })
            sys.exit(1)

        if errors:
            log(f"WARNING: {len(errors)} scene(s) failed and were skipped: {errors}")

        log(f"concatenating {len(scene_paths)} scenes into {part_name}.mp4")
        final_path = concat_scenes(scene_paths, batch_tmp, part_name)

        log("uploading finished part to Google Drive")
        service = get_drive_service()
        uploaded = upload_to_drive(service, final_path, f"{part_name}.mp4", output_folder_id)
        log(f"uploaded: {uploaded}")

        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a") as f:
                f.write(f"file_id={uploaded['id']}\n")
                f.write(f"file_name={uploaded['name']}\n")

        notify_n8n_resume({
            "status": "success",
            "part_name": part_name,
            "file_id": uploaded["id"],
            "file_name": uploaded["name"],
            "failed_scenes": errors,  # partial failures, if any, still surfaced
        })

    except Exception as e:
        log(f"FATAL: unhandled error in render job: {e}")
        notify_n8n_resume({
            "status": "error",
            "part_name": part_name,
            "error": str(e),
        })
        raise

    finally:
        shutil.rmtree(batch_tmp, ignore_errors=True)
        log("done")


if __name__ == "__main__":
    main()
