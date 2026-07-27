import subprocess
import os

def stitch_files():
    print("Preparing to merge files...")
    
    # Example logic for your segments:
    # FFmpeg merges video and audio seamlessly without re-encoding the video stream (lightning fast)
    video_input = "video_part1.mp4"
    audio_input = "audio_part1.mp3"
    output_output = "final_output1.mp4"
    
    cmd = [
        "ffmpeg", "-i", video_input, "-i", audio_input,
        "-c:v", "copy", "-c:a", "aac", output_output
    ]
    
    try:
        subprocess.run(cmd, check=True)
        print("Successfully stitched video!")
    except subprocess.CalledProcessError as e:
        print(f"Error during ffmpeg processing: {e}")

if __name__ == "__main__":
    stitch_files()
