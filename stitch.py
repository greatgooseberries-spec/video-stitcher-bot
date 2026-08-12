import os
import io
import re
import time
import socket
import ssl
import subprocess
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError

# Global socket timeout (seconds) so a stalled connection fails instead of
# hanging forever. This makes downloads actually hit our retry logic instead
# of sitting stuck on a dead connection.
socket.setdefaulttimeout(60)

DEST_FOLDER_ID = "1GZrZywT-c4DXIMMLeNuSNfSrjZ7b5aE4"

# TODO: replace with your actual metadata sheet's ID (the long string in its
# URL: https://docs.google.com/spreadsheets/d/THIS_PART/edit)
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1nYmebfH3jo7mxxnCI2ajx_ZDTjs9NrdBTkP-FN65O9s")

# How much shorter (in seconds) the video is allowed to be than the audio
# before we bother padding. Sub-second gaps are just encoding/rounding noise.
PAD_THRESHOLD_SEC = 1.0

# Reuse a single service object per API for the whole run instead of rebuilding per call
_drive_service = None
_sheets_service = None
_youtube_service = None


def run_cmd(command):
    print(f"Running: {' '.join(command)}")
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        raise RuntimeError("FFmpeg command failed.")
    return result.stdout


def get_duration_seconds(file_path):
    """Get media duration in seconds using ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokeys=1", file_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, TypeError):
        return None


def get_video_dimensions_and_fps(file_path):
    """Reads width/height/frame-rate off a video file's first video stream,
    so a black padding clip can be generated that matches it exactly."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "csv=p=0", file_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        width_str, height_str, fps_str = result.stdout.strip().split(",")
        return int(width_str), int(height_str), fps_str
    except Exception:
        return None, None, None


def pad_video_with_black(video_path, pad_duration, output_path):
    """
    Appends a black screen of `pad_duration` seconds onto the end of
    `video_path`. Used when the rendered video is shorter than the master
    narration track — instead of letting the final merge step (-shortest)
    silently cut the audio short, we extend the video so the *entire*
    narration always has something to play over, even if that's just a
    black screen for whatever portion is missing.

    This never raises on its own logic — if anything about it fails, the
    caller falls back to using the original (shorter) video rather than
    blocking the whole run.
    """
    width, height, fps = get_video_dimensions_and_fps(video_path)
    if not width or not height or not fps:
        width, height, fps = 1920, 1080, "30"
        print("WARNING: could not detect video dimensions/fps from the "
              "combined video — defaulting to 1920x1080@30 for the black pad.", flush=True)

    print(f"Video runs ~{pad_duration:.1f}s shorter than the narration — "
          f"generating a black screen pad so the full story audio is preserved.", flush=True)

    black_clip = "black_pad.mp4"
    run_cmd([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"color=c=black:s={width}x{height}:r={fps}:d={pad_duration:.2f}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast",
        black_clip
    ])

    pad_list = "pad_list.txt"
    with open(pad_list, "w") as f:
        f.write(f"file '{os.path.abspath(video_path)}'\n")
        f.write(f"file '{os.path.abspath(black_clip)}'\n")

    run_cmd([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", pad_list, "-c", "copy", output_path
    ])

    os.remove(pad_list)
    os.remove(black_clip)
    print(f"Padded video written to {output_path}", flush=True)
    return output_path


def ensure_video_covers_audio(video_path, audio_path):
    """
    Checks the combined video against the narration track. If the video is
    shorter, pads it with black so the story audio is never truncated.
    Returns the path to use going forward (the original path if no padding
    was needed or possible).
    """
    video_duration = get_duration_seconds(video_path)
    audio_duration = get_duration_seconds(audio_path)

    if not video_duration or not audio_duration:
        print("WARNING: could not determine video/audio duration — skipping "
              "the shortfall check and continuing with the video as-is.", flush=True)
        return video_path

    shortfall = audio_duration - video_duration
    if shortfall <= PAD_THRESHOLD_SEC:
        print(f"Video ({video_duration:.1f}s) already covers the full narration "
              f"({audio_duration:.1f}s) — no padding needed.", flush=True)
        return video_path

    try:
        # Small buffer on top of the exact shortfall so rounding/frame-boundary
        # differences can never leave the video a hair short of the audio.
        pad_duration = shortfall + 0.5
        padded_path = pad_video_with_black(video_path, pad_duration, "combined_video_padded.mp4")
        return padded_path
    except Exception as e:
        print(f"WARNING: failed to generate black-screen padding ({e}) — "
              f"continuing with the original (shorter) video instead of "
              f"failing the whole run.", flush=True)
        return video_path


def run_ffmpeg_with_progress(command, total_duration):
    """Run an ffmpeg command while streaming stderr and printing a live percentage.

    Keeps a rolling buffer of recent stderr lines so that if ffmpeg fails
    (including failing immediately, before any time= progress line is ever
    emitted) we can print the actual ffmpeg error instead of a bare
    "FFmpeg command failed." with no diagnostic info.
    """
    print(f"Running: {' '.join(command)}", flush=True)
    process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, bufsize=1
    )

    last_reported = -1
    time_pattern = re.compile(r"time=(\d+):(\d+):(\d+)\.\d+")
    recent_lines = []  # rolling buffer of last N stderr lines for error reporting
    max_buffer = 60

    for line in process.stderr:
        recent_lines.append(line.rstrip())
        if len(recent_lines) > max_buffer:
            recent_lines.pop(0)

        match = time_pattern.search(line)
        if match and total_duration:
            hours, minutes, seconds = map(int, match.groups())
            current_seconds = hours * 3600 + minutes * 60 + seconds
            percent = min(100, int((current_seconds / total_duration) * 100))
            if percent != last_reported and percent % 2 == 0:  # print every ~2%
                print(f"Progress: {percent}% ({current_seconds}s / {int(total_duration)}s)", flush=True)
                last_reported = percent

    process.wait()
    if process.returncode != 0:
        print("=== FFmpeg FAILED. Last output from ffmpeg (most recent lines): ===", flush=True)
        for line in recent_lines:
            print(line, flush=True)
        print("=== End of ffmpeg output ===", flush=True)
        raise RuntimeError(f"FFmpeg command failed with exit code {process.returncode}. See ffmpeg output above for the actual error.")
    print("Progress: 100% complete", flush=True)


def build_credentials(client_id_env, client_secret_env, refresh_token_env, scopes):
    return Credentials(
        None,
        refresh_token=os.environ[refresh_token_env],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ[client_id_env],
        client_secret=os.environ[client_secret_env],
        scopes=scopes
    )


def get_drive_service():
    """Drive + Sheets both live on the 'gg' account, using GDRIVE_REFRESH_TOKEN."""
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    credentials = build_credentials(
        "GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN",
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets.readonly"
        ]
    )
    _drive_service = build("drive", "v3", credentials=credentials)
    return _drive_service


def get_sheets_service():
    """Same 'gg' account credentials as Drive, different API surface (Sheets v4)."""
    global _sheets_service
    if _sheets_service is not None:
        return _sheets_service

    credentials = build_credentials(
        "GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN",
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets.readonly"
        ]
    )
    _sheets_service = build("sheets", "v4", credentials=credentials)
    return _sheets_service


def get_youtube_service():
    """YouTube channel lives on the 'im' account, using YOUTUBE_REFRESH_TOKEN.
    Reuses the same OAuth client (GDRIVE_CLIENT_ID/SECRET) — only the refresh
    token differs, since refresh tokens (not clients) are tied to an account."""
    global _youtube_service
    if _youtube_service is not None:
        return _youtube_service

    credentials = build_credentials(
        "GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
        scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    _youtube_service = build("youtube", "v3", credentials=credentials)
    return _youtube_service


def list_files_in_folder(folder_id):
    """List all non-trashed files in a Drive folder using the authenticated API (handles pagination)."""
    service = get_drive_service()
    files = []
    page_token = None

    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType)",
            pageToken=page_token,
            pageSize=1000
        ).execute()

        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return files


def download_file(file_id, dest_path, retries=3):
    """Download a single file via the authenticated Drive API, with basic retry on transient errors."""
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)

    for attempt in range(1, retries + 1):
        try:
            fh = io.FileIO(dest_path, "wb")
            # Larger chunk size (10MB) means fewer HTTP requests per file,
            # which helps avoid tripping Drive's per-100-second request quota
            # when downloading hundreds of files back to back.
            downloader = MediaIoBaseDownload(fh, request, chunksize=10 * 1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk(num_retries=2)
            fh.close()
            return
        except Exception as e:
            print(f"  Attempt {attempt} failed for {dest_path}: {e}")
            if attempt == retries:
                raise
            time.sleep(2 * attempt)  # simple backoff


def download_folder_contents(folder_id, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    print(f"Listing contents of Google Drive folder ID: {folder_id}")
    files = list_files_in_folder(folder_id)
    print(f"Found {len(files)} files. Downloading via authenticated Drive API...")

    for i, f in enumerate(files, 1):
        dest_path = os.path.join(output_dir, f["name"])
        print(f"[{i}/{len(files)}] Downloading {f['name']} ({f['id']})", flush=True)
        download_file(f["id"], dest_path)
        time.sleep(0.3)  # small pacing delay to avoid tripping Drive's per-100s rate quota

    print(f"Downloading contents from {folder_id} completed", flush=True)


def upload_to_drive(file_path, folder_id, file_name, max_attempts=5):
    """
    Upload a file to Drive as a resumable, chunked upload with retries.

    Two layers of resilience:
      1. `execute(num_retries=...)` — the googleapiclient library retries
         individual failed chunks internally (with backoff) for transient
         errors, including ssl.SSLError/SSLEOFError, socket errors, and
         common transient HTTP status codes. This is what was missing
         before: resumable=True alone does nothing without num_retries.
      2. An outer attempt loop — in case the resumable session itself dies
         completely (e.g. network drops for an extended period), we rebuild
         the upload object and start a fresh resumable session rather than
         failing the whole multi-hour pipeline run.
    """
    print(f"Uploading {file_path} to Drive folder {folder_id}...", flush=True)
    service = get_drive_service()
    file_metadata = {
        "name": file_name,
        "parents": [folder_id]
    }

    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            # Explicit chunksize (10MB) so this actually uploads in resumable
            # pieces instead of one giant request that has to be restarted
            # from zero on any interruption.
            media = MediaFileUpload(
                file_path,
                mimetype="video/mp4",
                resumable=True,
                chunksize=10 * 1024 * 1024
            )
            uploaded = service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id, webViewLink"
            ).execute(num_retries=10)

            file_id = uploaded.get("id")
            web_link = uploaded.get("webViewLink")

            # webViewLink can come back empty from the API depending on folder
            # permissions/timing. Fall back to constructing the standard Drive
            # view URL directly from the file_id, which always works.
            if not web_link and file_id:
                web_link = f"https://drive.google.com/file/d/{file_id}/view"
                print("webViewLink was empty in the API response, constructed fallback link from file_id.", flush=True)

            print(f"Upload complete. File ID: {file_id}", flush=True)
            print(f"Video link: {web_link}", flush=True)
            return file_id, web_link

        except (HttpError, ssl.SSLError, socket.error, ConnectionError, TimeoutError, OSError) as e:
            last_error = e
            print(f"  Upload attempt {attempt}/{max_attempts} failed: {e}", flush=True)
            if attempt == max_attempts:
                break
            sleep_time = min(60, 5 * (2 ** (attempt - 1)))  # 5s, 10s, 20s, 40s, capped at 60s
            print(f"  Retrying upload in {sleep_time}s...", flush=True)
            time.sleep(sleep_time)

    raise RuntimeError(f"Upload to Drive failed after {max_attempts} attempts: {last_error}")


def read_video_metadata(spreadsheet_id, cell_range="Sheet1!A2:C2"):
    """
    Read title/description/tags from the single live row in the metadata
    sheet. The sheet only ever holds one row (overwritten each run), so we
    always read the same fixed range rather than searching for a row.

    Falls back to generic placeholder metadata (never crashes the pipeline)
    if the sheet is unreachable or the row is empty/missing — a stale or
    missing spreadsheet shouldn't block an already-rendered video from
    being uploaded.
    """
    fallback_title = f"Story Video - {time.strftime('%Y-%m-%d %H:%M')}"
    try:
        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=cell_range
        ).execute()
        rows = result.get("values", [])
        row = rows[0] if rows else []

        title = row[0].strip() if len(row) > 0 and row[0].strip() else fallback_title
        description = row[1].strip() if len(row) > 1 else ""
        tags_raw = row[2].strip() if len(row) > 2 else ""

        # Tags column has been seen both comma-separated and space-separated —
        # split on either so it works regardless of which format is used.
        tags = [t for t in re.split(r"[,\s]+", tags_raw) if t]

        print(f"Metadata read from sheet — title: {title!r}, {len(tags)} tags", flush=True)
        return title, description, tags

    except Exception as e:
        print(f"WARNING: Failed to read metadata sheet ({e}). Using fallback title.", flush=True)
        return fallback_title, "", []


def upload_video_to_youtube(file_path, title, description, tags, privacy_status="private",
                             category_id="22", max_attempts=5):
    """
    Upload the finished video directly to YouTube from the local file on the
    runner — bypasses n8n entirely so it never has to load a multi-GB file
    into memory.

    NOTE: if the OAuth app (stitcher-desktop-client) hasn't completed
    Google's API verification/audit, YouTube forces uploaded videos to
    'private' regardless of what privacy_status is requested here. That's a
    platform policy on unverified apps, not a bug in this code.
    """
    print(f"Uploading to YouTube: {title!r} (privacy={privacy_status})", flush=True)
    service = get_youtube_service()

    body = {
        "snippet": {
            "title": title[:100],            # YouTube's hard title limit
            "description": description[:5000],  # YouTube's hard description limit
            "tags": tags[:500],
            "categoryId": category_id
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False
        }
    }

    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            media = MediaFileUpload(
                file_path,
                mimetype="video/mp4",
                resumable=True,
                chunksize=10 * 1024 * 1024
            )
            request = service.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media
            )
            response = request.execute(num_retries=10)
            video_id = response.get("id")
            video_url = f"https://youtu.be/{video_id}" if video_id else None

            print(f"YouTube upload complete. Video ID: {video_id}", flush=True)
            print(f"YouTube link: {video_url}", flush=True)
            return video_id, video_url

        except (HttpError, ssl.SSLError, socket.error, ConnectionError, TimeoutError, OSError) as e:
            last_error = e
            print(f"  YouTube upload attempt {attempt}/{max_attempts} failed: {e}", flush=True)
            if attempt == max_attempts:
                break
            sleep_time = min(60, 5 * (2 ** (attempt - 1)))
            print(f"  Retrying YouTube upload in {sleep_time}s...", flush=True)
            time.sleep(sleep_time)

    raise RuntimeError(f"YouTube upload failed after {max_attempts} attempts: {last_error}")


def notify_n8n(file_id, web_link, youtube_video_id=None, youtube_url=None):
    webhook_url = "https://lordkiwi.app.n8n.cloud/webhook/416a64ff-7e1c-45a3-af73-dc413876305e"
    if not web_link:
        print("WARNING: web_link is empty/None — n8n will receive a null video_link!", flush=True)
    payload = {
        "status": "success",
        # Kept unchanged for backward compatibility with any existing n8n
        # nodes that already reference these two field names.
        "message": "Video stitching complete. Uploaded to Drive and YouTube!",
        "file_id": file_id,
        "video_link": web_link,
        # New fields for the direct YouTube upload.
        "youtube_video_id": youtube_video_id,
        "youtube_url": youtube_url
    }
    print(f"Webhook payload: {payload}", flush=True)
    print("Notifying n8n via production webhook...", flush=True)
    try:
        response = requests.post(webhook_url, json=payload, timeout=30)
        print(f"Webhook response status: {response.status_code}")
        if response.status_code >= 400:
            print(f"Webhook response body: {response.text}")
            raise RuntimeError(f"n8n webhook returned {response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"Error: Failed to reach n8n webhook: {e}")
        raise


def notify_n8n_failure(error_message, stage="unknown"):
    """Ping the same n8n webhook with an error status so a crashed run is never silent."""
    webhook_url = "https://lordkiwi.app.n8n.cloud/webhook/416a64ff-7e1c-45a3-af73-dc413876305e"
    payload = {
        "status": "error",
        "message": f"Video stitching FAILED at stage '{stage}': {error_message}",
        "file_id": None,
        "video_link": None
    }
    print(f"Notifying n8n of FAILURE via production webhook... payload={payload}", flush=True)
    try:
        response = requests.post(webhook_url, json=payload, timeout=30)
        print(f"Failure webhook response status: {response.status_code}", flush=True)
    except requests.exceptions.RequestException as e:
        # At this point we're already failing - don't let a webhook error
        # mask the original exception, just log it.
        print(f"Also failed to reach n8n failure webhook: {e}", flush=True)


def main():
    video_folder_id = os.getenv("VIDEO_FOLDER_ID", "1G9Gmc-VeAzy13bAO95xW9go7Z43R-HAA")
    voice_folder_id = os.getenv("VOICE_FOLDER_ID", "1ph8ZfknTc5N5GGVkCW8rHQOS_NAFgG39")

    print("=== STAGE: Downloading video parts ===", flush=True)
    download_folder_contents(video_folder_id, "video_downloads")
    print("=== STAGE: Downloading voice track ===", flush=True)
    download_folder_contents(voice_folder_id, "voice_downloads")

    video_files = []
    for root, dirs, files in os.walk("video_downloads"):
        for file in files:
            if file.lower().endswith(".mp4"):
                video_files.append(os.path.join(root, file))
    video_files.sort()

    if not video_files:
        raise RuntimeError("No video parts found in the downloaded folder!")

    print(f"=== STAGE: Preparing to concatenate {len(video_files)} video parts ===", flush=True)
    list_filename = "file_list.txt"
    with open(list_filename, "w") as f:
        for vf in video_files:
            f.write(f"file '{os.path.abspath(vf)}'\n")

    print("=== STAGE: Concatenating video parts together (should be fast, no re-encode) ===", flush=True)
    run_cmd([
        "ffmpeg", "-f", "concat", "-safe", "0",
        "-i", list_filename, "-c", "copy", "combined_video.mp4"
    ])
    print("=== STAGE: Concatenation complete ===", flush=True)

    audio_file = None
    known_audio_ext = (".mp3", ".wav", ".m4a")
    for root, dirs, files in os.walk("voice_downloads"):
        for file in files:
            if file.lower().endswith(known_audio_ext):
                audio_file = os.path.join(root, file)
                break
        if audio_file:
            break

    # Fallback: the voice folder should only ever contain the single master
    # narration file. If it has no recognizable extension (e.g. uploaded as
    # "full_voice" with no suffix), just take whatever single file is there.
    if not audio_file:
        for root, dirs, files in os.walk("voice_downloads"):
            if files:
                audio_file = os.path.join(root, files[0])
                break

    if not audio_file:
        raise RuntimeError("No audio file found in the voice folder!")

    print(f"Using audio file: {audio_file}", flush=True)

    # === STAGE: Make sure the video is never shorter than the narration ===
    # If scenes are missing (failed renders, quota errors, whatever), the
    # combined video can end up shorter than the master audio track. The
    # final merge step below uses -shortest, which would otherwise silently
    # cut the narration off at the video's length — chopping the story in
    # half with no error anywhere. Instead, pad the video out with a black
    # screen so the full narration always has something to play over.
    print("=== STAGE: Checking video length against narration length ===", flush=True)
    video_for_merge = ensure_video_covers_audio("combined_video.mp4", audio_file)

    # --- Subtitles (optional) ---
    subtitle_file = None
    subtitles_folder_id = os.getenv("SUBTITLES_FOLDER_ID", "1pMJPxMmkuyMfanNRhcYUz0XHS9MbLTDM")
    if subtitles_folder_id:
        print("=== STAGE: Downloading subtitles ===", flush=True)
        download_folder_contents(subtitles_folder_id, "subtitle_downloads")
        for root, dirs, files in os.walk("subtitle_downloads"):
            for file in files:
                if file.lower().endswith(".srt"):
                    subtitle_file = os.path.join(root, file)
                    break
            if subtitle_file:
                break
        if subtitle_file:
            print(f"Using subtitle file: {subtitle_file}", flush=True)
        else:
            print("SUBTITLES_FOLDER_ID was set but no .srt file was found — continuing without subtitles.", flush=True)

    print("=== STAGE: Merging video with master audio (this is the slow re-encode step, can take 10-25+ min for a long video) ===", flush=True)
    total_duration = get_duration_seconds(video_for_merge)
    if total_duration:
        print(f"Video duration: ~{int(total_duration // 60)} min {int(total_duration % 60)} sec", flush=True)

    ffmpeg_cmd = [
        "ffmpeg", "-i", video_for_merge, "-i", audio_file,
    ]

    if subtitle_file:
        # Bottom-aligned (Alignment=2), yellow text (&H0000FFFF in ASS BGR
        # order), medium black outline (Outline=2), font size 32.
        srt_escaped = subtitle_file.replace("\\", "/").replace(":", "\\:")
        style = (
            "FontSize=16,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,"
            "BorderStyle=1,Outline=2,Shadow=0,Alignment=2,Bold=1"
        )
        ffmpeg_cmd += ["-vf", f"subtitles='{srt_escaped}':force_style='{style}'"]

    ffmpeg_cmd += [
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest",
        "-c:v", "libx264", "-preset", "fast", "-c:a", "aac",
        "final_master_output.mp4"
    ]

    run_ffmpeg_with_progress(ffmpeg_cmd, total_duration)
    print("=== STAGE: Stitching complete! ===", flush=True)

    print("=== STAGE: Reading video metadata from sheet ===", flush=True)
    title, description, tags = read_video_metadata(SPREADSHEET_ID)

    print("=== STAGE: Uploading final video to YouTube ===", flush=True)
    youtube_video_id, youtube_url = upload_video_to_youtube(
        "final_master_output.mp4", title, description, tags
    )

    print("=== STAGE: Uploading final video to Drive (backup) ===", flush=True)
    file_id, web_link = upload_to_drive("final_master_output.mp4", DEST_FOLDER_ID, "full story.mp4")

    notify_n8n(file_id, web_link, youtube_video_id, youtube_url)


if __name__ == "__main__":
    import sys
    import traceback

    try:
        main()
    except Exception as e:
        print("=== STAGE: FATAL ERROR - pipeline did not complete ===", flush=True)
        traceback.print_exc()
        notify_n8n_failure(str(e))
        sys.exit(1)
