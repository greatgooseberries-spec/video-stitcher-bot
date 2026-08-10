import os
import io
import time
import socket
import subprocess
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# Global socket timeout (seconds) so a stalled connection fails instead of
# hanging forever. This makes downloads actually hit our retry logic instead
# of sitting stuck on a dead connection.
socket.setdefaulttimeout(60)

DEST_FOLDER_ID = "1GZrZywT-c4DXIMMLeNuSNfSrjZ7b5aE4"

# Reuse a single Drive service for the whole run instead of building a new one per call
_drive_service = None


def run_cmd(command):
    print(f"Running: {' '.join(command)}")
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        raise RuntimeError("FFmpeg command failed.")
    return result.stdout


def get_drive_service():
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    credentials = Credentials(
        None,
        refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GDRIVE_CLIENT_ID"],
        client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    _drive_service = build("drive", "v3", credentials=credentials)
    return _drive_service


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


def upload_to_drive(file_path, folder_id, file_name):
    print(f"Uploading {file_path} to Drive folder {folder_id}...")
    service = get_drive_service()
    file_metadata = {
        "name": file_name,
        "parents": [folder_id]
    }
    media = MediaFileUpload(file_path, mimetype="video/mp4", resumable=True)
    uploaded = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webViewLink"
    ).execute()
    file_id = uploaded.get("id")
    web_link = uploaded.get("webViewLink")
    print(f"Upload complete. File ID: {file_id}")
    return file_id, web_link


def notify_n8n(file_id, web_link):
    webhook_url = "https://lordkiwi.app.n8n.cloud/webhook/416a64ff-7e1c-45a3-af73-dc413876305e"
    payload = {
        "status": "success",
        "message": "Video stitching complete. Ready for YouTube upload!",
        "file_id": file_id,
        "video_link": web_link
    }
    print("Notifying n8n via production webhook...")
    try:
        response = requests.post(webhook_url, json=payload, timeout=30)
        print(f"Webhook response status: {response.status_code}")
        if response.status_code >= 400:
            print(f"Webhook response body: {response.text}")
            raise RuntimeError(f"n8n webhook returned {response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"Error: Failed to reach n8n webhook: {e}")
        raise


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

    print("=== STAGE: Merging video with master audio (this is the slow re-encode step, can take 10-25+ min for a long video) ===", flush=True)
    run_cmd([
        "ffmpeg", "-i", "combined_video.mp4", "-i", audio_file,
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest",
        "-c:v", "libx264", "-preset", "fast", "-c:a", "aac",
        "final_master_output.mp4"
    ])
    print("=== STAGE: Stitching complete! ===", flush=True)

    print("=== STAGE: Uploading final video to Drive ===", flush=True)

    file_id, web_link = upload_to_drive("final_master_output.mp4", DEST_FOLDER_ID, "full story.mp4")
    notify_n8n(file_id, web_link)


if __name__ == "__main__":
    main()
