import argparse
import cv2
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from flask import Flask, Response, jsonify, render_template_string, request, send_file

parser = argparse.ArgumentParser(description="Raspberry Pi motion camera")
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
                    help="ALSA input device, e.g. plughw:1,0")
parser.add_argument("--no-audio", action="store_true",
                    help="Record video without audio")
args = parser.parse_args()

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
camera_error = None

camera = cv2.VideoCapture(args.camera)
camera.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
camera.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
camera.set(cv2.CAP_PROP_FPS, args.fps)

PAGE = r'''<!doctype html>
<html><head><meta name="viewport" content="width=device-width"><title>Pi Motion Camera</title>
<style>body{font-family:sans-serif;background:#111;color:#eee;margin:0;padding:1rem}img{max-width:100%;background:#000}.badge{display:inline-block;padding:.25rem .5rem;border-radius:.25rem;background:#555}.live{background:#b11}table{border-collapse:collapse;margin-top:1rem}td,th{padding:.5rem;border-bottom:1px solid #444}a{color:#7cf}button{background:#922;color:white;border:0;padding:.4rem .7rem;cursor:pointer}</style></head>
<body><h1>Pi Motion Camera</h1><p>Status: <span id="status" class="badge">Loading</span></p><p id="error"></p>
<img src="/stream" alt="Live camera"><h2>Recordings</h2><table id="files"><tr><th>Name</th><th>Size</th><th>Actions</th></tr></table>
<script>async function update(){try{const s=await (await fetch('/api/status')).json();const el=document.getElementById('status');el.textContent=s.recording?'RECORDING':(s.motion?'MOTION':'Idle');el.className='badge '+(s.recording?'live':(s.motion?'live':''));document.getElementById('error').textContent=s.camera_error||'';const f=await (await fetch('/api/recordings')).json();const table=document.getElementById('files');table.innerHTML='<tr><th>Name</th><th>Size</th><th>Actions</th></tr>'+f.map(x=>'<tr><td>'+x.name+'</td><td>'+x.size_mb+' MB</td><td><a href="/recordings/'+encodeURIComponent(x.name)+'">Download</a> <button onclick="removeRecording('+JSON.stringify(x.name)+')">Delete</button></td></tr>').join('')}catch(e){document.getElementById('error').textContent='Unable to refresh camera status'}}async function removeRecording(name){if(!confirm('Permanently delete '+name+'?'))return;const r=await fetch('/api/recordings/'+encodeURIComponent(name),{method:'DELETE'});if(!r.ok){const data=await r.json();alert(data.error||'Could not delete recording');return}update()}update();setInterval(update,3000);</script></body></html>'''


def start_audio(path):
    """Capture an independent ALSA audio track; it never triggers motion."""
    global audio_log
    if args.no_audio:
        return None
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is missing; install it with: sudo apt install ffmpeg")
    audio_log = tempfile.TemporaryFile(mode="w+b")
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-thread_queue_size", "512", "-f", "alsa", "-i", args.audio_device,
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        "-f", "wav", "-y", str(path),
    ]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=audio_log)
    except Exception:
        audio_log.close()
        audio_log = None
        raise
    # Give FFmpeg a moment to fail fast on an unavailable device.
    time.sleep(0.25)
    if process.poll() is not None:
        message = read_audio_log()
        raise RuntimeError("Audio capture could not start" + (f": {message}" if message else ""))
    return process


def read_audio_log():
    global audio_log
    if audio_log is None:
        return ""
    try:
        audio_log.flush()
        audio_log.seek(0)
        return audio_log.read().decode("utf-8", errors="replace")[-500:].strip()
    finally:
        audio_log.close()
        audio_log = None


def finish_recording():
    """Close temporary tracks, mux audio into the final MP4, then clean up."""
    global writer, recording, audio_process, audio_log, video_temp_path, audio_temp_path
    global camera_error

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
        details = read_audio_log()
        # FFmpeg commonly uses a non-zero status when stopped gracefully by SIGINT.
        audio_ok = (audio_temp_path is not None and audio_temp_path.exists()
                    and audio_temp_path.stat().st_size > 44)
        if not audio_ok:
            camera_error = "Audio capture produced no usable WAV file" + (f": {details}" if details else "")

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
                camera_error = "Could not combine audio and video: " + result.stderr[-500:]
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
        with lock:
            recording = False


def capture_loop():
    global latest_frame, motion_active, last_motion, recording, writer
    global recording_path, video_temp_path, audio_temp_path, audio_process, camera_error
    previous = None

    while True:
        ok, frame = camera.read()
        if not ok:
            camera_error = "Could not read from the camera"
            time.sleep(1)
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)
        motion = False
        if previous is not None:
            delta = cv2.absdiff(previous, gray)
            _, thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)
            thresh = cv2.dilate(thresh, None, iterations=2)
            changed = np.count_nonzero(thresh) / thresh.size * 100.0
            motion = changed >= args.sensitivity
        previous = gray
        now = time.monotonic()
        if motion:
            last_motion = now

        with lock:
            latest_frame = frame.copy()
            motion_active = motion

        # Keep writing on every frame until the full cooldown has elapsed from
        # the last detected visual movement. UI motion state is not used here.
        if motion and not recording:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            recording_path = recordings_dir / f"motion-{stamp}.mp4"
            video_temp_path = recordings_dir / f".motion-{stamp}.video.mp4"
            audio_temp_path = recordings_dir / f".motion-{stamp}.audio.wav"
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(str(video_temp_path), fourcc, args.fps,
                                     (frame.shape[1], frame.shape[0]))
            if not writer.isOpened():
                writer.release()
                writer = None
                camera_error = "Could not create video file"
                video_temp_path = None
                audio_temp_path = None
            else:
                with lock:
                    recording = True
                try:
                    audio_process = start_audio(audio_temp_path)
                    if audio_process is None and not args.no_audio:
                        camera_error = "Audio capture is not available"
                    elif args.no_audio:
                        camera_error = None
                except (OSError, RuntimeError) as exc:
                    camera_error = str(exc)
                    audio_process = None
        if recording and writer is not None:
            writer.write(frame)
            if audio_process is not None and audio_process.poll() is not None:
                details = read_audio_log()
                camera_error = "Audio capture stopped unexpectedly" + (f": {details}" if details else "")
                audio_process = None

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
            ok, encoded = cv2.imencode(".jpg", frame,
                                       [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" +
                       encoded.tobytes() + b"\r\n")
            time.sleep(1.0 / args.fps)
    return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/status")
def status():
    with lock:
        return jsonify(motion=motion_active, recording=recording,
                       camera_error=camera_error,
                       recording_file=recording_path.name if recording_path else None)


@app.route("/api/recordings", methods=["GET"])
def recordings():
    files = sorted(recordings_dir.glob("motion-*.mp4"), key=lambda p: p.stat().st_mtime,
                    reverse=True)
    return jsonify([{"name": p.name,
                     "size_mb": round(p.stat().st_size / 1024 / 1024, 2)}
                    for p in files if p.is_file()])


@app.route("/api/recordings/<path:name>", methods=["DELETE"])
def delete_recording(name):
    global camera_error
    # Limit deletion to completed recordings created by this application.
    if Path(name).name != name or not name.startswith("motion-") or not name.endswith(".mp4"):
        return jsonify(error="Invalid recording name"), 400
    path = (recordings_dir / name).resolve()
    with lock:
        if path == recording_path and recording:
            return jsonify(error="Cannot delete the recording while it is being recorded"), 409
        if recordings_dir not in path.parents or not path.is_file():
            return jsonify(error="Recording not found"), 404
        try:
            path.unlink()
        except OSError as exc:
            camera_error = f"Could not delete recording: {exc}"
            return jsonify(error="Could not delete recording"), 500
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
