import argparse
import cv2
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from flask import Flask, Response, jsonify, render_template_string, send_file

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
                    help="Seconds to keep recording after last motion")
parser.add_argument("--recordings", default="recordings")
args = parser.parse_args()

recordings_dir = Path(args.recordings).resolve()
recordings_dir.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
lock = threading.Lock()
latest_frame = None
motion_active = False
last_motion = 0.0
recording = False
writer = None
recording_path = None
camera_error = None

camera = cv2.VideoCapture(args.camera)
camera.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
camera.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
camera.set(cv2.CAP_PROP_FPS, args.fps)

PAGE = r'''<!doctype html>
<html><head><meta name="viewport" content="width=device-width"><title>Pi Motion Camera</title>
<style>body{font-family:sans-serif;background:#111;color:#eee;margin:0;padding:1rem}img{max-width:100%;background:#000}.badge{display:inline-block;padding:.25rem .5rem;border-radius:.25rem;background:#555}.live{background:#b11}table{border-collapse:collapse;margin-top:1rem}td,th{padding:.5rem;border-bottom:1px solid #444}a{color:#7cf}</style></head>
<body><h1>Pi Motion Camera</h1><p>Status: <span id="status" class="badge">Loading</span></p>
<img src="/stream" alt="Live camera"><h2>Recordings</h2><table id="files"><tr><th>Name</th><th>Size</th><th>Download</th></tr></table>
<script>async function update(){const s=await (await fetch('/api/status')).json();const el=document.getElementById('status');el.textContent=s.recording?'RECORDING':(s.motion?'MOTION':'Idle');el.className='badge '+(s.recording?'live':(s.motion?'live':''));const f=await (await fetch('/api/recordings')).json();const table=document.getElementById('files');table.innerHTML='<tr><th>Name</th><th>Size</th><th>Download</th></tr>'+f.map(x=>'<tr><td>'+x.name+'</td><td>'+x.size_mb+' MB</td><td><a href="/recordings/'+encodeURIComponent(x.name)+'" target="_blank">Open</a></td></tr>').join('')}update();setInterval(update,2000);</script></body></html>'''


def capture_loop():
    global latest_frame, motion_active, last_motion, recording, writer, recording_path, camera_error
    previous = None

    while True:
        ok, frame = camera.read()
        if not ok:
            camera_error = "Could not read from the camera"
            time.sleep(1)
            continue

        camera_error = None
        motion = False
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if previous is not None:
            delta = cv2.absdiff(previous, gray)
            _, thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)
            thresh = cv2.dilate(thresh, None, iterations=2)
            changed = np.count_nonzero(thresh) / thresh.size * 100.0
            motion = changed >= args.sensitivity

        previous = gray
        if motion:
            last_motion = time.monotonic()

        with lock:
            latest_frame = frame.copy()
            motion_active = motion or (time.monotonic() - last_motion < 1.0)

        if motion_active:
            if not recording:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                recording_path = recordings_dir / f"motion-{stamp}.mp4"
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                writer = cv2.VideoWriter(str(recording_path), fourcc, args.fps,
                                         (frame.shape[1], frame.shape[0]))
                recording = writer.isOpened()
                if not recording:
                    camera_error = "Could not create video file"
            if recording:
                writer.write(frame)
        elif recording and time.monotonic() - last_motion >= args.cooldown:
            writer.release()
            writer = None
            recording = False

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


@app.route("/api/recordings")
def recordings():
    files = sorted(recordings_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime,
                    reverse=True)
    return jsonify([{"name": p.name,
                     "size_mb": round(p.stat().st_size / 1024 / 1024, 2)}
                    for p in files])


@app.route("/recordings/<path:name>")
def recording_file(name):
    path = (recordings_dir / name).resolve()
    if recordings_dir not in path.parents:
        return "Not found", 404
    if not path.is_file():
        return "Not found", 404
    return send_file(path, mimetype="video/mp4", as_attachment=True)


thread = threading.Thread(target=capture_loop, daemon=True)
thread.start()
app.run(host=args.host, port=args.port, threaded=True)
