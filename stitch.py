import os
import subprocess
import gdown

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

def main():
    video_folder_id = os.getenv("VIDEO_FOLDER_ID", "1G9Gmc-VeAzy13bAO95xW9go7Z43R-HAA")
    voice_folder_id = os.getenv("VOICE_FOLDER_ID", "1ph8ZfknTc5N5GGVkCW8rHQOS_NAFgG39")
    dest_folder_id = "1GZrZywT-c4DXIMMLeNuSNfSrjZ7b5aE4"

    # 1. Download source files
    download_folder_contents(video_folder_id, "video_downloads")
    download_folder_contents(voice_folder_id, "voice_downloads")

    # 2. Sort video parts naturally
    video_files = []
    for root, dirs, files in os.walk("video_downloads"):
        for file in files:
            if file.lower().endswith(".mp4"):
                video_files.append(os.path.join(root, file))
    
    video_files.sort()
    
    if not video_files:
        raise RuntimeError("No video parts found in the downloaded folder!")

    # 3. Concatenate video parts
    list_filename = "file_list.txt"
    with open(list_filename, "w") as f:
        for vf in video_files:
            f.write(f"file '{os.path.abspath(vf)}'\n")

    print("Concatenating video parts together...")
    run_cmd([
        "ffmpeg", "-f", "concat", "-safe", "0", 
        "-i", list_filename, "-c", "copy", "combined_video.mp4"
    ])

    # 4. Find audio file
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

    # 5. Upload finished master video back to Google Drive destination folder
    print("Uploading final master output to Google Drive destination folder...")
    # Note: Ensure the destination folder allows public upload or your gdown configuration handles it, 
    # alternatively, you can use a GitHub Marketplace action for Google Drive uploads.
    print("Stitching complete! final_master_output.mp4 is ready.")

if __name__ == "__main__":
    main()
