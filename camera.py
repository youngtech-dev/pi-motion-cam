import argparse
import cv2
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from flask import Flask, Response, jsonify, render_template_string, send_file

parser = argparse.ArgumentParser(description="Headless Raspberry Pi motion camera")
parser.add_argument("--camera", type=int, default=0)
parser.add_argument("--host", default="0.0.0.0")
parser.add_argument("--port", type=int, default=8080)
parser.add_argument("--width", type=int, default=640)
parser.add_argument("--height", type=int, default=480)
parser.add_argument("--fps", type=int, default=15)
parser.add_argument("--sensitivity", type=float, default=25.0,
                    help="Motion threshold percentage (lower = more sensitive)")
parser.add_argument("--cooldown", type=float, default=10.0,
                    help="Seconds to keep recording after last visual motion")
parser.add_argument("--recordings", default="recordings")
parser.add_argument("--audio-device", default="default",
                    help="ALSA capture device, e.g. plughw:CARD=Device,DEV=0")
parser.add_argument("--audio-rate", type=int, default=16000,
                    help="Requested ALSA sample rate (plughw can resample)")
parser.add_argument("--no-audio", action="store_true",
                    help="Record video without audio")
parser.add_argument("--test-audio", action="store_true",
                    help="Capture a short audio test and exit")
parser.add_argument("--audio-test-seconds", type=int, default=5)
args = parser.parse_args()


def test_audio_device():
    """Exercise the configured ALSA device before starting the camera server."""
    if not shutil.which("ffmpeg"):
        print("Audio test failed: ffmpeg is not installed (sudo apt install ffmpeg).", file=sys.stderr)
        return 1
    fd, filename = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "alsa", "-sample_rate", str(args.audio_rate), "-channels", "1",
            "-i", args.audio_device, "-t", str(max(1, args.audio_test_seconds)),
            "-vn", "-ac", "1", "-ar", str(args.audio_rate), "-c:a", "pcm_s16le",
            "-y", filename,
        ]
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=max(15, args.audio_test_seconds + 10))
        if result.returncode or not os.path.exists(filename) or os.path.getsize(filename) <= 44:
            print("Audio test failed for ALSA device " + repr(args.audio_device) + ":", file=sys.stderr)
            print((result.stderr or "No audio samples were captured.").strip(), file=sys.stderr)
            print("Check the device with `arecord -l` and test it with `arecord -D DEVICE -f S16_LE -r 16000 -c 1 -d 5 /tmp/test.wav`.", file=sys.stderr)
            return 1
        print(f"Audio test passed for {args.audio_device}; captured {os.path.getsize(filename)} bytes.")
        return 0
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Audio test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            os.unlink(filename)
        except OSError:
            pass


if args.test_audio:
    raise SystemExit(test_audio_device())

recordings_dir = Path(args.recordings).resolve()
recordings_dir.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
lock = threading.RLock()
latest_frame = None
motion_active = False
last_motion = 0.0
recording = False
writer = None
recording_path = None
video_temp_path = None
audio_temp_path = None
audio_process = None
audio_log = None
audio_failed = False
camera_error = None

camera = cv2.VideoCapture(args.camera)
camera.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
camera.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
camera.set(cv2.CAP_PROP_FPS, args.fps)

PAGE = r'''<!doctype html>
<html><head><meta name="viewport" content="width=device-width"><title>Pi Motion Camera</title>
<style>body{font-family:sans-serif;background:#111;color:#eee;margin:0;padding:1rem}img{max-width:100%;background:#000}.badge{display:inline-block;padding:.25rem .5rem;border-radius:.25rem;background:#555}.live{background:#b11}table{border-collapse:collapse;margin-top:1rem}td,th{padding:.5rem;border-bottom:1px solid #444}a{color:#7cf}button{background:#922;color:white;border:0;padding:.4rem .7rem;cursor:pointer}.error{color:#ff9b9b;white-space:pre-wrap}</style></head>
<body><h1>Pi Motion Camera</h1><p>Status: <span id="status" class="badge">Loading</span></p>
<p id="error" class="error" role="status"></p><img src="/stream" alt="Live camera">
<h2>Recordings</h2><table><thead><tr><th>Name</th><th>Size</th><th>Actions</th></tr></thead><tbody id="files"></tbody></table>
<script>
async function removeRecording(name, button){
  if(!window.confirm('Permanently delete '+name+'?')) return;
  button.disabled=true;
  try {
    const response=await fetch('/api/recordings/'+encodeURIComponent(name),{method:'DELETE'});
    const body=await response.json();
    if(!response.ok) throw new Error(body.error||('Delete failed ('+response.status+')'));
    await updateRecordings();
  } catch(error) {
    document.getElementById('error').textContent='Delete failed: '+error.message;
    button.disabled=false;
  }
}
async function updateRecordings(){
  const response=await fetch('/api/recordings',{cache:'no-store'});
  if(!response.ok) throw new Error('Could not load recordings ('+response.status+')');
  const files=await response.json();
  const body=document.getElementById('files');
  body.replaceChildren();
  for(const file of files){
    const row=document.createElement('tr');
    const name=document.createElement('td'); name.textContent=file.name;
    const size=document.createElement('td'); size.textContent=file.size_mb+' MB';
    const actions=document.createElement('td');
    const download=document.createElement('a'); download.textContent='Download';
    download.href='/recordings/'+encodeURIComponent(file.name);
    const remove=document.createElement('button'); remove.textContent='Delete';
    remove.addEventListener('click',()=>removeRecording(file.name,remove));
    actions.append(download,document.createTextNode(' '),remove);
    row.append(name,size,actions); body.append(row);
  }
}
async function update(){
  try{
    const response=await fetch('/api/status',{cache:'no-store'});
    const s=await response.json();
    const badge=document.getElementById('status');
    badge.textContent=s.recording?'RECORDING':(s.motion?'MOTION':'Idle');
    badge.className='badge '+(s.recording||s.motion?'live':'');
    document.getElementById('error').textContent=s.camera_error||'';
    await updateRecordings();
  }catch(error){document.getElementById('error').textContent='Web server request failed: '+error.message;}
}
update();setInterval(update,3000);
</script></body></html>'''


def start_audio(path):
    """Record an independent ALSA track. Audio is never used for motion detection."""
    global audio_log
    if args.no_audio:
        return None
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is missing; install with `sudo apt install ffmpeg`.")
    audio_log = tempfile.TemporaryFile(mode="w+b")
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-thread_queue_size", "512", "-f", "alsa", "-sample_rate", str(args.audio_rate),
        "-channels", "1", "-i", args.audio_device, "-vn", "-ac", "1",
        "-ar", str(args.audio_rate), "-c:a", "pcm_s16le", "-f", "wav", "-y", str(path),
    ]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=audio_log)
    except OSError:
        audio_log.close()
        audio_log = None
        raise
    time.sleep(0.5)
    if process.poll() is not None:
        details = read_audio_log(close=True)
        raise RuntimeError("ALSA audio capture failed for " + repr(args.audio_device) +
                           (f": {details}" if details else ". Run `python camera.py --audio-device DEVICE --test-audio` to diagnose."))
    return process


def read_audio_log(close=False):
    global audio_log
    if audio_log is None:
        return ""
    try:
        audio_log.flush()
        audio_log.seek(0)
        return audio_log.read().decode("utf-8", errors="replace")[-1500:].strip()
    finally:
        if close:
            audio_log.close()
            audio_log = None


def finish_recording():
    """Finalize video/audio tracks and publish the completed MP4."""
    global writer, recording, audio_process, video_temp_path, audio_temp_path
    global camera_error, audio_failed
    if writer is not None:
        writer.release()
        writer = None

    audio_ok = False
    if audio_process is not None:
        process = audio_process
        audio_process = None
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        details = read_audio_log(close=True)
        audio_ok = (audio_temp_path is not None and audio_temp_path.exists()
                    and audio_temp_path.stat().st_size > 44)
        if not audio_ok and not audio_failed:
            camera_error = "No audio samples were saved" + (f": {details}" if details else "")

    try:
        if video_temp_path is None or not video_temp_path.exists():
            return
        if audio_ok:
            command = [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(video_temp_path), "-i", str(audio_temp_path),
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                "-shortest", "-movflags", "+faststart", str(recording_path),
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                camera_error = "Audio/video mux failed; saving silent video: " + result.stderr[-1000:]
                os.replace(video_temp_path, recording_path)
        else:
            os.replace(video_temp_path, recording_path)
    except (OSError, subprocess.SubprocessError) as exc:
        camera_error = f"Could not finalize recording: {exc}"
    finally:
        for path in (video_temp_path, audio_temp_path):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        video_temp_path = None
        audio_temp_path = None
        audio_failed = False
        with lock:
            recording = False


def capture_loop():
    global latest_frame, motion_active, last_motion, recording, writer
    global recording_path, video_temp_path, audio_temp_path, audio_process
    global camera_error, audio_failed
    previous = None

    while True:
        ok, frame = camera.read()
        if not ok:
            camera_error = "Could not read from the camera"
            time.sleep(1)
            continue

        gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (21, 21), 0)
        motion = False
        if previous is not None:
            delta = cv2.absdiff(previous, gray)
            _, thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)
            thresh = cv2.dilate(thresh, None, iterations=2)
            motion = np.count_nonzero(thresh) / thresh.size * 100.0 >= args.sensitivity
        previous = gray
        now = time.monotonic()
        if motion:
            last_motion = now

        with lock:
            latest_frame = frame.copy()
            motion_active = motion

        if motion and not recording:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            recording_path = recordings_dir / f"motion-{stamp}.mp4"
            video_temp_path = recordings_dir / f".motion-{stamp}.video.mp4"
            audio_temp_path = recordings_dir / f".motion-{stamp}.audio.wav"
            writer = cv2.VideoWriter(str(video_temp_path), cv2.VideoWriter_fourcc(*"mp4v"),
                                     args.fps, (frame.shape[1], frame.shape[0]))
            if not writer.isOpened():
                writer.release()
                writer = None
                camera_error = "Could not create video file"
                video_temp_path = None
                audio_temp_path = None
            else:
                with lock:
                    recording = True
                audio_failed = False
                if not args.no_audio:
                    try:
                        audio_process = start_audio(audio_temp_path)
                        camera_error = None
                    except (OSError, RuntimeError) as exc:
                        audio_process = None
                        audio_failed = True
                        camera_error = str(exc)

        if recording and writer is not None:
            writer.write(frame)
            if audio_process is not None and audio_process.poll() is not None and not audio_failed:
                details = read_audio_log()
                audio_failed = True
                camera_error = "ALSA audio process stopped unexpectedly" + (f": {details}" if details else "")

        # This timeout is checked on every captured frame and is based only on
        # the last visual motion timestamp. Continued motion resets the timer.
        if recording and now - last_motion >= args.cooldown:
            finish_recording()

        time.sleep(max(0.001, 1.0 / args.fps))


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/stream")
def stream():
    def frames():
        while True:
            with lock:
                frame = None if latest_frame is None else latest_frame.copy()
            if frame is None:
                time.sleep(0.05)
                continue
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + encoded.tobytes() + b"\r\n"
            time.sleep(1.0 / args.fps)
    return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/status")
def status():
    with lock:
        audio_capturing = audio_process is not None and audio_process.poll() is None
        return jsonify(motion=motion_active, recording=recording,
                       camera_error=camera_error, audio_device=args.audio_device,
                       audio_capturing=audio_capturing,
                       recording_file=recording_path.name if recording_path else None)


@app.route("/api/recordings", methods=["GET"])
def recordings():
    files = sorted(recordings_dir.glob("motion-*.mp4"), key=lambda p: p.stat().st_mtime,
                    reverse=True)
    return jsonify([{"name": p.name, "size_mb": round(p.stat().st_size / 1024 / 1024, 2)}
                    for p in files if p.is_file()])


@app.route("/api/recordings/<path:name>", methods=["DELETE", "POST"])
def delete_recording(name):
    global camera_error
    if Path(name).name != name or not name.startswith("motion-") or not name.endswith(".mp4"):
        return jsonify(error="Invalid recording name"), 400
    path = (recordings_dir / name).resolve()
    with lock:
        if path == recording_path and recording:
            return jsonify(error="Cannot delete a recording while it is being recorded"), 409
        if recordings_dir not in path.parents or not path.is_file():
            return jsonify(error="Recording not found"), 404
        try:
            path.unlink()
        except OSError as exc:
            camera_error = f"Could not delete recording: {exc}"
            return jsonify(error=camera_error), 500
    return jsonify(deleted=name)


@app.route("/recordings/<path:name>")
def recording_file(name):
    path = (recordings_dir / name).resolve()
    if recordings_dir not in path.parents or not path.is_file():
        return "Not found", 404
    return send_file(path, mimetype="video/mp4", as_attachment=True)


thread = threading.Thread(target=capture_loop, daemon=True)
thread.start()
app.run(host=args.host, port=args.port, threaded=True)
