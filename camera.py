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
parser.add_argument("--sensitivity", type=float, default=25.0)
parser.add_argument("--cooldown", type=float, default=10.0,
                    help="Seconds to keep recording after last visual motion")
parser.add_argument("--recordings", default="recordings")
parser.add_argument("--audio-device", default="default")
parser.add_argument("--audio-rate", type=int, default=16000)
parser.add_argument("--no-audio", action="store_true")
parser.add_argument("--test-audio", action="store_true")
parser.add_argument("--audio-test-seconds", type=int, default=5)
args = parser.parse_args()


def test_audio_device():
    if not shutil.which("ffmpeg"):
        print("ffmpeg is missing", file=sys.stderr)
        return 1
    fd, filename = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                   "-f", "alsa", "-sample_rate", str(args.audio_rate), "-channels", "1",
                   "-i", args.audio_device, "-t", str(args.audio_test_seconds), "-y", filename]
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=args.audio_test_seconds + 10)
        if result.returncode != 0 or os.path.getsize(filename) <= 44:
            print(result.stderr or "No audio samples captured", file=sys.stderr)
            return 1
        print("Audio test passed")
        return 0
    except (OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
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
camera_error = None

camera = cv2.VideoCapture(args.camera)
camera.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
camera.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
camera.set(cv2.CAP_PROP_FPS, args.fps)

PAGE = r'''<!doctype html>
<html><head><meta name="viewport" content="width=device-width"><title>Pi Motion Camera</title>
<style>body{font-family:sans-serif;background:#111;color:#eee;margin:0;padding:1rem}img{max-width:100%;background:#000}.badge{display:inline-block;padding:.25rem .5rem;border-radius:.25rem;background:#555}.live{background:#b11}table{border-collapse:collapse;margin-top:1rem}td,th{padding:.5rem;border-bottom:1px solid #444}a{color:#7cf}button{background:#922;color:white;border:0;padding:.4rem .7rem;cursor:pointer}</style></head>
<body><h1>Pi Motion Camera</h1><p>Status: <span id="status" class="badge">Loading</span></p><p id="error"></p>
<img src="/stream" alt="Live camera"><h2>Recordings</h2><table><thead><tr><th>Name</th><th>Size</th><th>Actions</th></tr></thead><tbody id="files"></tbody></table>
<script>
async function update(){
 try{const s=await (await fetch('/api/status',{cache:'no-store'})).json();
  const badge=document.getElementById('status');badge.textContent=s.recording?'RECORDING':(s.motion?'MOTION':'Idle');badge.className='badge '+(s.recording||s.motion?'live':'');document.getElementById('error').textContent=s.camera_error||'';
  const files=await (await fetch('/api/recordings',{cache:'no-store'})).json();const body=document.getElementById('files');body.replaceChildren();
  for(const f of files){const row=document.createElement('tr');const a=document.createElement('a');a.href='/recordings/'+encodeURIComponent(f.name);a.textContent='Download';const d=document.createElement('button');d.textContent='Delete';d.onclick=async()=>{if(confirm('Permanently delete '+f.name+'?')){const r=await fetch('/api/recordings/'+encodeURIComponent(f.name),{method:'DELETE'});if(r.ok)update();else alert((await r.json()).error||'Delete failed')}};const c=document.createElement('td');c.append(a,document.createTextNode(' '),d);row.append(Object.assign(document.createElement('td'),{textContent:f.name}),Object.assign(document.createElement('td'),{textContent:f.size_mb+' MB'}),c);body.append(row)}
 }catch(e){document.getElementById('error').textContent='Web request failed: '+e.message}}
update();setInterval(update,2000);
</script></body></html>'''


def start_audio(path):
    global audio_log
    if args.no_audio:
        return None
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed")
    audio_log = tempfile.TemporaryFile(mode="w+b")
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
               "-f", "alsa", "-sample_rate", str(args.audio_rate), "-channels", "1",
               "-i", args.audio_device, "-t", str(60 * 60), "-vn", "-ac", "1",
               "-ar", str(args.audio_rate), "-c:a", "pcm_s16le", "-f", "wav", "-y", str(path)]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=audio_log)
    time.sleep(0.5)
    if process.poll() is not None:
        audio_log.flush(); audio_log.seek(0)
        details = audio_log.read().decode("utf-8", errors="replace")[-1000:].strip()
        audio_log.close(); audio_log = None
        raise RuntimeError("ALSA capture failed" + (f": {details}" if details else ""))
    return process


def audio_details():
    global audio_log
    if audio_log is None:
        return ""
    try:
        audio_log.flush(); audio_log.seek(0)
        return audio_log.read().decode("utf-8", errors="replace")[-1000:].strip()
    finally:
        audio_log.close(); audio_log = None


def finish_recording():
    global writer, recording, audio_process, video_temp_path, audio_temp_path, camera_error
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
            process.kill(); process.wait()
        details = audio_details()
        audio_ok = audio_temp_path is not None and audio_temp_path.exists() and audio_temp_path.stat().st_size > 44
        if not audio_ok:
            camera_error = "No usable audio captured" + (f": {details}" if details else "")
    try:
        if video_temp_path is None or not video_temp_path.exists():
            return
        if audio_ok:
            result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                                     "-i", str(video_temp_path), "-i", str(audio_temp_path),
                                     "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                                     "-shortest", "-movflags", "+faststart", str(recording_path)],
                                    capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                camera_error = "Mux failed; saved silent video: " + result.stderr[-500:]
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
        gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (21, 21), 0)
        motion = False
        if previous is not None:
            delta = cv2.absdiff(previous, gray)
            _, thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)
            changed = np.count_nonzero(cv2.dilate(thresh, None, iterations=2)) / thresh.size * 100.0
            motion = changed >= args.sensitivity
        previous = gray
        now = time.monotonic()

        # Reset the timeout on every frame that the new algorithm classifies as motion.
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
                writer.release(); writer = None
                camera_error = "Could not create video file"
                video_temp_path = None; audio_temp_path = None
            else:
                with lock:
                    recording = True
                if not args.no_audio:
                    try:
                        audio_process = start_audio(audio_temp_path)
                        camera_error = None
                    except (OSError, RuntimeError) as exc:
                        audio_process = None
                        camera_error = str(exc)

        # Crucial: write every frame while recording, including frames classified as idle.
        if recording and writer is not None:
            writer.write(frame)

        elapsed = now - last_motion
        if recording and elapsed >= args.cooldown:
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
                time.sleep(0.05); continue
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + encoded.tobytes() + b"\r\n"
            time.sleep(1.0 / args.fps)
    return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/status")
def status():
    with lock:
        return jsonify(motion=motion_active, recording=recording,
                       camera_error=camera_error,
                       recording_file=recording_path.name if recording_path else None,
                       cooldown_seconds=args.cooldown)


@app.route("/api/recordings", methods=["GET"])
def recordings():
    files = sorted(recordings_dir.glob("motion-*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonify([{"name": p.name, "size_mb": round(p.stat().st_size / 1024 / 1024, 2)}
                    for p in files if p.is_file()])


@app.route("/api/recordings/<path:name>", methods=["DELETE"])
def delete_recording(name):
    if Path(name).name != name or not name.startswith("motion-") or not name.endswith(".mp4"):
        return jsonify(error="Invalid recording name"), 400
    path = (recordings_dir / name).resolve()
    with lock:
        if path == recording_path and recording:
            return jsonify(error="Recording is still in progress"), 409
        if recordings_dir not in path.parents or not path.is_file():
            return jsonify(error="Recording not found"), 404
        try:
            path.unlink()
        except OSError as exc:
            return jsonify(error=f"Could not delete recording: {exc}"), 500
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
