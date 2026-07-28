import os
import subprocess
import requests
import gdown
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

DEST_FOLDER_ID = "1GZrZywT-c4DXIMMLeNuSNfSrjZ7b5aE4"

def run_cmd(command):
    print(f"Running: {' '.join(command)}")
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        raise RuntimeError("FFmpeg command failed.")
    return result.stdout

def download_folder_contents(folder_id, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    print(f"Downloading contents from Google Drive folder ID: {folder_id}")
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    gdown.download_folder(url, output=output_dir, quiet=False, use_cookies=False)

def get_drive_service():
    credentials = Credentials(
        None,
        refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GDRIVE_CLIENT_ID"],
        client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=credentials)

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

    download_folder_contents(video_folder_id, "video_downloads")
    download_folder_contents(voice_folder_id, "voice_downloads")

    video_files = []
    for root, dirs, files in os.walk("video_downloads"):
        for file in files:
            if file.lower().endswith(".mp4"):
                video_files.append(os.path.join(root, file))
    video_files.sort()

    if not video_files:
        raise RuntimeError("No video parts found in the downloaded folder!")

    list_filename = "file_list.txt"
    with open(list_filename, "w") as f:
        for vf in video_files:
            f.write(f"file '{os.path.abspath(vf)}'\n")

    print("Concatenating video parts together...")
    run_cmd([
        "ffmpeg", "-f", "concat", "-safe", "0",
        "-i", list_filename, "-c", "copy", "combined_video.mp4"
    ])

    audio_file = None
    for root, dirs, files in os.walk("voice_downloads"):
        for file in files:
            if file.lower().endswith((".mp3", ".wav", ".m4a")):
                audio_file = os.path.join(root, file)
                break

    if not audio_file:
        raise RuntimeError("No audio file found in the voice folder!")

    print("Merging video with master audio and padding end if audio is longer...")
    run_cmd([
        "ffmpeg", "-i", "combined_video.mp4", "-i", audio_file,
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest",
        "-c:v", "libx264", "-preset", "fast", "-c:a", "aac",
        "final_master_output.mp4"
    ])
    print("Stitching complete!")

    file_id, web_link = upload_to_drive("final_master_output.mp4", DEST_FOLDER_ID, "full story.mp4")
    notify_n8n(file_id, web_link)

if __name__ == "__main__":
    main()
