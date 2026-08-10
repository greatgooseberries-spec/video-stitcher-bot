#!/usr/bin/env python3
"""
Render Scenes -> Final Video
Reads the job payload from env vars (set by render-scenes.yml via
repository_dispatch client_payload), builds one Ken Burns pan/zoom clip per
scene (background + character overlay(s) + trimmed audio segment), stitches
them into a final video, uploads it to Google Drive, then POSTs a callback
webhook so n8n can pick it up (matches the existing Webhook -> Download file
-> Merge4 -> Upload a video chain: the callback must include "video_link").
"""

import json
import os
import subprocess
import sys
import hashlib
import requests
from pathlib import Path
from PIL import Image

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ---------- config ----------
FPS = 30
OUT_W, OUT_H = 1920, 1080
CRF = "18"
PRESET = "medium"

WORK_DIR = Path("work")
BG_DIR = WORK_DIR / "backgrounds"
COMPOSITE_DIR = WORK_DIR / "composites"
CLIPS_DIR = WORK_DIR / "clips"
for d in (WORK_DIR, BG_DIR, COMPOSITE_DIR, CLIPS_DIR):
    d.mkdir(parents=True, exist_ok=True)


def log(msg):
    print(f"[render] {msg}", flush=True)


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def download(url: str, dest: Path) -> Path:
    if dest.exists():
        return dest
    log(f"Downloading {url} -> {dest.name}")
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest


# ---------- image compositing ----------

def get_background(bg_url: str) -> Path:
    """Download + resize/cover a background image once, cached by URL."""
    dest = BG_DIR / f"{url_hash(bg_url)}.png"
    if dest.exists():
        return dest

    raw = download(bg_url, BG_DIR / f"{url_hash(bg_url)}_raw")
    img = Image.open(raw).convert("RGB")

    # cover-fit to OUT_W x OUT_H
    src_ratio = img.width / img.height
    dst_ratio = OUT_W / OUT_H
    if src_ratio > dst_ratio:
        new_h = OUT_H
        new_w = int(new_h * src_ratio)
    else:
        new_w = OUT_W
        new_h = int(new_w / src_ratio)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - OUT_W) // 2
    top = (new_h - OUT_H) // 2
    img = img.crop((left, top, left + OUT_W, top + OUT_H))
    img.save(dest)
    raw.unlink(missing_ok=True)
    return dest


def get_character(char_url: str) -> Path:
    dest = BG_DIR / f"char_{url_hash(char_url)}.png"
    if dest.exists():
        return dest
    download(char_url, dest)
    return dest


def composite_scene_image(bg_url: str, character_urls: list) -> Path:
    """Composite background + up to 3 characters, cached by combo."""
    combo_key = url_hash(bg_url + "|" + "|".join(sorted(character_urls)))
    dest = COMPOSITE_DIR / f"{combo_key}.png"
    if dest.exists():
        return dest

    bg_path = get_background(bg_url)
    canvas = Image.open(bg_path).convert("RGBA")

    n = len(character_urls)
    if n > 0:
        char_h = int(OUT_H * 0.85)
        slot_w = OUT_W // n
        for i, curl in enumerate(character_urls[:3]):
            try:
                cpath = get_character(curl)
                char_img = Image.open(cpath).convert("RGBA")
                ratio = char_h / char_img.height
                char_img = char_img.resize(
                    (max(1, int(char_img.width * ratio)), char_h), Image.LANCZOS
                )
                x = i * slot_w + (slot_w - char_img.width) // 2
                y = OUT_H - char_h
                canvas.alpha_composite(char_img, (x, y))
            except Exception as e:
                log(f"WARNING: failed to composite character {curl}: {e}")

    canvas.convert("RGB").save(dest)
    return dest


# ---------- per-scene clip rendering ----------

def render_scene_clip(scene: dict, audio_path: Path, out_path: Path):
    if out_path.exists():
        return out_path

    image_path = composite_scene_image(scene["background_url"], scene["character_urls"])
    duration = max(0.5, float(scene["duration"]))
    start = float(scene["start"])

    total_frames = max(1, round(duration * FPS))
    zoompan = (
        f"zoompan=z='min(zoom+0.0007,1.3)':d={total_frames}:"
        f"s={OUT_W}x{OUT_H}:fps={FPS}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(image_path),
        "-ss", str(start), "-t", str(duration), "-i", str(audio_path),
        "-filter_complex", f"[0:v]{zoompan},format=yuv420p[v]",
        "-map", "[v]", "-map", "1:a",
        "-t", str(duration),
        "-c:v", "libx264", "-preset", PRESET, "-crf", CRF,
        "-c:a", "aac", "-b:a", "192k",
        "-r", str(FPS),
        str(out_path),
    ]
    log(f"Rendering scene {scene['scene_index']} ({duration:.2f}s)")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(result.stderr[-3000:])
        raise RuntimeError(f"ffmpeg failed on scene {scene['scene_index']}")
    return out_path


# ---------- concat ----------

def concat_clips(clip_paths: list, final_path: Path):
    concat_file = WORK_DIR / "concat.txt"
    with open(concat_file, "w") as f:
        for p in clip_paths:
            f.write(f"file '{p.resolve()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-c:v", "libx264", "-preset", PRESET, "-crf", CRF,
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        str(final_path),
    ]
    log("Concatenating all scene clips into final video")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(result.stderr[-3000:])
        raise RuntimeError("ffmpeg concat failed")
    return final_path


# ---------- google drive ----------

def get_drive_service():
    creds = Credentials(
        None,
        refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
        client_id=os.environ["GDRIVE_CLIENT_ID"],
        client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(GoogleAuthRequest())
    return build("drive", "v3", credentials=creds)


def upload_to_drive(service, file_path: Path, filename: str, folder_id: str) -> str:
    log(f"Uploading {filename} to Drive folder {folder_id}")
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(str(file_path), mimetype="video/mp4", resumable=True)
    uploaded = service.files().create(
        body=file_metadata, media_body=media, fields="id"
    ).execute()
    file_id = uploaded["id"]
    log(f"Uploaded. Drive file id: {file_id}")
    return file_id


# ---------- webhook callback ----------

def notify_webhook(webhook_url: str, job_id: str, file_id: str, status="completed", error=None):
    if not webhook_url or webhook_url == "PASTE_YOUR_N8N_WEBHOOK_URL_HERE":
        log("WARNING: no real callback_webhook_url provided, skipping notify")
        return
    payload = {
        "job_id": job_id,
        "status": status,
        "video_link": f"https://drive.google.com/file/d/{file_id}/view" if file_id else None,
        "error": error,
    }
    try:
        r = requests.post(webhook_url, json=payload, timeout=30)
        log(f"Webhook notified, status {r.status_code}")
    except Exception as e:
        log(f"WARNING: failed to notify webhook: {e}")


# ---------- main ----------

def main():
    job_id = os.environ["JOB_ID"]
    output_filename = os.environ.get("OUTPUT_FILENAME", f"{job_id}.mp4")
    master_audio_url = os.environ["MASTER_AUDIO_URL"]
    callback_webhook_url = os.environ.get("CALLBACK_WEBHOOK_URL", "")
    scenes_json_url = os.environ["SCENES_JSON_URL"]
    folder_id = os.environ["GDRIVE_OUTPUT_FOLDER_ID"]

    # Download scenes payload JSON from Google Drive
    scenes_file_path = download(scenes_json_url, WORK_DIR / "scenes.json")
    scenes = json.loads(scenes_file_path.read_text(encoding="utf-8"))

    log(f"Job {job_id}: {len(scenes)} scenes")

    audio_path = download(master_audio_url, WORK_DIR / "master_audio.mp3")

    try:
        clip_paths = []
        for scene in scenes:
            out_path = CLIPS_DIR / f"scene_{scene['scene_index']:05d}.mp4"
            render_scene_clip(scene, audio_path, out_path)
            clip_paths.append(out_path)

        final_path = WORK_DIR / output_filename
        concat_clips(clip_paths, final_path)

        service = get_drive_service()
        file_id = upload_to_drive(service, final_path, output_filename, folder_id)

        notify_webhook(callback_webhook_url, job_id, file_id, status="completed")
        log("Done.")

    except Exception as e:
        log(f"ERROR: {e}")
        notify_webhook(callback_webhook_url, job_id, None, status="failed", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
