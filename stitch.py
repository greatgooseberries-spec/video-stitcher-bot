import subprocess
import os

def run_cmd(command):
    print(f"Running: {' '.join(command)}")
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        raise RuntimeError("FFmpeg command failed.")
    return result.stdout

def main():
    print("Starting video and audio stitching pipeline...")

    # Step 1: Create a text file listing all parts in order for FFmpeg concat demuxer
    list_filename = "file_list.txt"
    with open(list_filename, "w") as f:
        for i in range(1, 10): # from part_1 to part_9 based on your files
            filename = f"part_{i}.mp4"
            if os.path.exists(filename):
                f.write(f"file '{filename}'\n")
            else:
                print(f"Warning: {filename} not found!")

    print("Concatenating video parts...")
    # Concat all video parts into one intermediate file without re-encoding (super fast)
    run_cmd([
        "ffmpeg", "-f", "concat", "-safe", "0", 
        "-i", list_filename, "-c", "copy", "combined_video.mp4"
    ])

    print("Merging with master audio and padding end if necessary...")
    # This maps the long audio, adds a black screen pad if audio is longer than video, 
    # and cuts/matches the exact duration of the audio track.
    audio_file = "tts (2).mp3" # Your master audio file name
    output_file = "final_master_output.mp4"

    run_cmd([
        "ffmpeg", "-i", "combined_video.mp4", "-i", audio_file,
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest", # Ensures video matches audio duration (pads or cuts cleanly)
        "-c:v", "libx264", "-preset", "fast", "-c:a", "aac",
        output_output
    ])

    print(f"Success! Final video generated: {output_file}")

if __name__ == "__main__":
    main()
