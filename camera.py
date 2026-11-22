import argparse
import cv2
import hmac
import os
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from functools import wraps
from pathlib import Path

import numpy as np
from flask import (Flask, Response, abort, jsonify, redirect, render_template_string,
                   request, send_file, session, url_for)

parser = argparse.ArgumentParser(description="Headless Raspberry Pi motion camera")
parser.add_argument("--camera", type=int, default=0)
parser.add_argument("--host", default="0.0.0.0")
parser.add_argument("--port", type=int, default=8080)
parser.add_argument("--width", type=int, default=640)
parser.add_argument("--height", type=int, default=480)
parser.add_argument("--fps", type=int, default=15)
parser.add_argument("--sensitivity", type=float, default=25.0)
parser.add_argument("--cooldown", type=float, default=40.0,
                    help="Seconds after the last detected visual motion")
parser.add_argument("--pre-roll", type=float, default=5.0)
parser.add_argument("--recordings", default="recordings")
parser.add_argument("--audio-device", default="plughw:1,0")
parser.add_argument("--audio-rate", type=int, default=16000)
parser.add_argument("--no-audio", action="store_true")
parser.add_argument("--test-audio", action="store_true")
parser.add_argument("--audio-test-seconds", type=int, default=5)
parser.add_argument("--pin", default=os.environ.get("PI_CAM_PIN"),
                    help="Four-digit web PIN; preferably set PI_CAM_PIN in environment")
parser.add_argument("--secret-key", default=os.environ.get("PI_CAM_SECRET_KEY"),
                    help="Flask session signing key; set PI_CAM_SECRET_KEY in environment")
args = parser.parse_args()


def test_audio_device():
    if not shutil.which("ffmpeg"):
        print("ffmpeg is missing", file=sys.stderr)
        return 1
    fd, filename = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-f", "alsa", "-sample_rate", str(args.audio_rate), "-channels", "1",
               "-i", args.audio_device, "-t", str(args.audio_test_seconds), "-y", filename]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=args.audio_test_seconds + 10)
        if result.returncode or os.path.getsize(filename) <= 44:
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
if not args.pin or len(args.pin) != 4 or not args.pin.isdigit():
    parser.error("Set PI_CAM_PIN or --pin to exactly four digits")

recordings_dir = Path(args.recordings).resolve()
recordings_dir.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)
app.secret_key = args.secret_key or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("PI_CAM_COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
)
lock = threading.RLock()
latest_frame = None
latest_jpeg = None
frame_id = 0
frame_condition = threading.Condition()
motion_active = False
last_motion = 0.0
recording = False
recording_path = None
video_temp_path = None
audio_temp_path = None
audio_process = None
audio_log = None
audio_error = None
camera_error = None
writer_queue = None
writer_thread = None
pre_roll = queue.deque(maxlen=max(1, round(args.fps * max(0.0, args.pre_roll)))) if hasattr(queue, "deque") else None
# deque is imported from collections; keep the rolling buffer bounded by requested preroll.
from collections import deque
pre_roll = deque(maxlen=max(1, round(args.fps * max(0.0, args.pre_roll))))
pre_roll_seconds_for_clip = 0.0
login_attempts = {}
login_attempts_lock = threading.Lock()

camera = cv2.VideoCapture(args.camera)
camera.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
camera.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
camera.set(cv2.CAP_PROP_FPS, args.fps)
camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)

STYLE = """
:root{color-scheme:dark;--bg:#0b1220;--panel:#111c2e;--line:#25354d;--text:#edf4ff;--muted:#9aabc3;--accent:#55c2b2;--danger:#ff7777}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#172741 0,var(--bg) 58%);color:var(--text);font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;min-height:100vh}
.wrap{width:min(100% - 32px,1000px);margin:0 auto;padding:36px 0}.brand{font-weight:750;letter-spacing:.02em}.eyebrow{color:var(--accent);font-size:.76rem;text-transform:uppercase;letter-spacing:.15em;font-weight:700}.muted{color:var(--muted)}
.card{background:linear-gradient(145deg,rgba(20,33,53,.96),rgba(15,25,42,.96));border:1px solid var(--line);border-radius:18px;padding:24px;box-shadow:0 18px 50px #0003}.center{max-width:430px;margin:9vh auto}.field{width:100%;margin:12px 0 16px;padding:14px;border:1px solid #354760;border-radius:10px;background:#0b1423;color:var(--text);font-size:1.2rem;letter-spacing:.35em;text-align:center}
.btn{display:inline-flex;align-items:center;justify-content:center;text-decoration:none;border:0;border-radius:10px;padding:11px 16px;background:var(--accent);color:#08201d;font-weight:750;cursor:pointer}.btn.secondary{background:#263b56;color:var(--text)}.btn.danger{background:#612d38;color:#ffe8e8}.btn:disabled{opacity:.55;cursor:wait}.error{color:#ffaaaa;min-height:1.5em}.top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px;margin-top:22px}.tile{display:block;color:inherit;text-decoration:none;transition:transform .15s,border-color .15s}.tile:hover{transform:translateY(-3px);border-color:var(--accent)}
video,img.stream{display:block;width:100%;max-height:75vh;object-fit:contain;background:#03070c;border:1px solid var(--line);border-radius:14px}.status{display:inline-flex;gap:8px;align-items:center;padding:6px 11px;background:#1a2a3e;border-radius:99px;color:var(--muted)}.dot{width:9px;height:9px;border-radius:50%;background:#8090a6}.dot.live{background:#ff6767;box-shadow:0 0 12px #ff6767}
table{width:100%;border-collapse:collapse;margin-top:18px}td,th{text-align:left;padding:13px 10px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-size:.85rem}td.actions{white-space:nowrap}.actions .btn{margin-right:7px;padding:8px 11px;font-size:.9rem}.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center}@media(max-width:600px){.wrap{padding:22px 0}.top{align-items:flex-start}.card{padding:18px}td,th{padding:10px 6px;font-size:.9rem}}
"""

LOGIN_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · Pi Motion Cam</title><style>''' + STYLE + r'''</style></head>
<body><main class="wrap"><section class="card center"><div class="eyebrow">Private camera</div><h1 class="brand">Pi Motion Cam</h1><p class="muted">Enter your four-digit PIN to continue.</p><form method="post" action="{{ url_for('login') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label class="muted" for="pin">Camera PIN</label><input class="field" id="pin" name="pin" type="password" inputmode="numeric" pattern="[0-9]{4}" maxlength="4" minlength="4" autocomplete="one-time-code" required autofocus><button class="btn" type="submit" style="width:100%">Unlock camera</button></form><p class="error" role="alert">{{ error }}</p><p class="muted" style="font-size:.85rem">Keep this service on a trusted network. Use HTTPS if accessing it remotely.</p></section></main></body></html>'''

HOME_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Home · Pi Motion Cam</title><style>''' + STYLE + r'''</style></head>
<body><main class="wrap"><header class="top"><div><div class="eyebrow">Camera dashboard</div><h1 class="brand">Pi Motion Cam</h1></div><form method="post" action="{{ url_for('logout') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn secondary" type="submit">Lock</button></form></header>
<p class="muted">Choose where you want to go.</p><div class="grid"><a class="card tile" href="{{ url_for('live_page') }}"><div class="eyebrow">Watch</div><h2>Live stream</h2><p class="muted">View the camera feed and current motion status.</p></a><a class="card tile" href="{{ url_for('recordings_page') }}"><div class="eyebrow">Review</div><h2>Recordings</h2><p class="muted">Browse, download, or remove saved clips.</p></a></div></main></body></html>'''

LIVE_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Live · Pi Motion Cam</title><style>''' + STYLE + r'''</style></head>
<body><main class="wrap"><header class="top"><div><div class="eyebrow">Live camera</div><h1 class="brand">Stream</h1></div><a class="btn secondary" href="{{ url_for('home') }}">Home</a></header><div class="toolbar" style="margin-bottom:14px"><span class="status"><span id="dot" class="dot"></span><span id="status">Connecting…</span></span><span id="error" class="error"></span></div><img class="stream" src="{{ url_for('stream') }}" alt="Live camera stream"><p class="muted">Live video is streamed without audio.</p></main>
<script>async function refresh(){try{const r=await fetch('{{ url_for('status') }}',{cache:'no-store'});const s=await r.json();document.getElementById('status').textContent=s.recording?'Recording':(s.motion?'Motion detected':'Monitoring');document.getElementById('dot').className='dot '+(s.recording||s.motion?'live':'');document.getElementById('error').textContent=s.camera_error||''}catch(e){document.getElementById('status').textContent='Status unavailable'}}refresh();setInterval(refresh,2000);</script></body></html>'''

RECORDINGS_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Recordings · Pi Motion Cam</title><style>''' + STYLE + r'''</style></head>
<body><main class="wrap"><header class="top"><div><div class="eyebrow">Saved clips</div><h1 class="brand">Recordings</h1></div><a class="btn secondary" href="{{ url_for('home') }}">Home</a></header><section class="card"><div class="toolbar"><button class="btn" id="refresh" type="button">Refresh recordings</button><span id="message" class="muted">Click refresh to load recordings.</span></div><div style="overflow-x:auto"><table><thead><tr><th>Recording</th><th>Size</th><th>Actions</th></tr></thead><tbody id="files"></tbody></table></div></section></main>
<script>
const message=document.getElementById('message');
async function loadFiles(){const button=document.getElementById('refresh');button.disabled=true;message.textContent='Loading…';try{const response=await fetch('{{ url_for('recordings_api') }}',{cache:'no-store'});if(!response.ok)throw new Error('Request failed ('+response.status+')');const files=await response.json();const body=document.getElementById('files');body.replaceChildren();if(!files.length){message.textContent='No recordings found.';return}for(const file of files){const row=document.createElement('tr');const name=document.createElement('td');name.textContent=file.name;const size=document.createElement('td');size.textContent=file.size_mb+' MB';const actions=document.createElement('td');actions.className='actions';const download=document.createElement('a');download.className='btn secondary';download.href='/recordings/'+encodeURIComponent(file.name);download.textContent='Download';const remove=document.createElement('button');remove.className='btn danger';remove.textContent='Delete';remove.addEventListener('click',async()=>{if(!confirm('Permanently delete '+file.name+'?'))return;remove.disabled=true;try{const result=await fetch('/api/recordings/'+encodeURIComponent(file.name),{method:'DELETE',headers:{'X-CSRF-Token':'{{ csrf_token }}'}});const data=await result.json();if(!result.ok)throw new Error(data.error||'Delete failed');message.textContent='Recording deleted.';await loadFiles()}catch(e){message.textContent=e.message;remove.disabled=false}});actions.append(download,document.createTextNode(' '),remove);row.append(name,size,actions);body.append(row)}message.textContent=files.length+' recording(s).'}catch(e){message.textContent='Could not load recordings: '+e.message}finally{button.disabled=false}}
document.getElementById('refresh').addEventListener('click',loadFiles);
</script></body></html>'''


def csrf_token():
    token = session.get("csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def login_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/") or request.path == "/stream":
                return jsonify(error="Authentication required"), 401
            return redirect(url_for("login"))
        return function(*args, **kwargs)
    return wrapped


def csrf_required():
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    return bool(expected) and hmac.compare_digest(supplied, expected)


def login_rate_limited(ip):
    now = time.monotonic()
    with login_attempts_lock:
        attempts = [t for t in login_attempts.get(ip, []) if now - t < 300]
        login_attempts[ip] = attempts
        return len(attempts) >= 5


def record_login_failure(ip):
    with login_attempts_lock:
        login_attempts.setdefault(ip, []).append(time.monotonic())


def add_timestamp(frame):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + f".{int(time.monotonic() * 10) % 10}"
    cv2.rectangle(frame, (8, 8), (330, 45), (0, 0, 0), cv2.FILLED)
    cv2.putText(frame, stamp, (14, 35), cv2.FONT_HERSHEY_SIMPLEX,
                0.75, (255, 255, 255), 2, cv2.LINE_AA)


def write_frames(video_writer, frames_queue):
    while True:
        frame = frames_queue.get()
        if frame is None:
            break
        video_writer.write(frame)
    video_writer.release()


def start_audio(path):
    global audio_log
    if args.no_audio:
        return None
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed")
    audio_log = tempfile.TemporaryFile(mode="w+b")
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
               "-thread_queue_size", "512", "-f", "alsa", "-sample_rate", str(args.audio_rate),
               "-channels", "1", "-i", args.audio_device, "-vn", "-ac", "1",
               "-ar", str(args.audio_rate), "-c:a", "pcm_s16le", "-f", "wav", "-y", str(path)]
    return subprocess.Popen(command, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=audio_log)


def close_audio_log():
    global audio_log
    if audio_log is None:
        return ""
    try:
        audio_log.flush()
        audio_log.seek(0)
        return audio_log.read().decode("utf-8", errors="replace")[-1000:].strip()
    finally:
        audio_log.close()
        audio_log = None


def finish_recording():
    global recording, writer_queue, writer_thread, audio_process
    global video_temp_path, audio_temp_path, camera_error, audio_error
    frames_queue, thread = writer_queue, writer_thread
    writer_queue = writer_thread = None
    if frames_queue is not None:
        frames_queue.put(None)
    if thread is not None:
        thread.join()
    audio_ok = False
    if audio_process is not None:
        process, audio_process = audio_process, None
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        details = close_audio_log()
        audio_ok = audio_temp_path is not None and audio_temp_path.exists() and audio_temp_path.stat().st_size > 44
        if not audio_ok:
            camera_error = "No usable audio captured" + (f": {details}" if details else "")
    try:
        if video_temp_path is None or not video_temp_path.exists():
            return
        if audio_ok:
            delay_ms = round(pre_roll_seconds_for_clip * 1000)
            result = subprocess.run([
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(video_temp_path), "-i", str(audio_temp_path),
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                "-af", f"adelay={delay_ms}:all=1", "-movflags", "+faststart",
                str(recording_path)], capture_output=True, text=True, timeout=180)
            if result.returncode:
                camera_error = "Audio/video mux failed; saving full silent video: " + result.stderr[-500:]
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
        video_temp_path = audio_temp_path = None
        audio_error = None
        with lock:
            recording = False


def capture_loop():
    global latest_frame, latest_jpeg, frame_id, motion_active, last_motion, recording
    global recording_path, video_temp_path, audio_temp_path, audio_process
    global writer_queue, writer_thread, camera_error, audio_error, pre_roll_seconds_for_clip
    previous_gray = None
    while True:
        ok, frame = camera.read()
        if not ok:
            camera_error = "Could not read from the camera"
            time.sleep(0.2)
            continue
        gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (21, 21), 0)
        motion = False
        if previous_gray is not None:
            delta = cv2.absdiff(previous_gray, gray)
            _, thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)
            changed = np.count_nonzero(cv2.dilate(thresh, None, iterations=2)) / thresh.size * 100.0
            motion = changed >= args.sensitivity
        previous_gray = gray
        now = time.monotonic()
        add_timestamp(frame)
        if motion:
            last_motion = now
        with lock:
            latest_frame = frame.copy()
            motion_active = motion
        ok_jpeg, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if ok_jpeg:
            with frame_condition:
                latest_jpeg = encoded.tobytes()
                frame_id += 1
                frame_condition.notify_all()

        pre_roll.append(frame.copy())
        started = False
        if motion and not recording:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            recording_path = recordings_dir / f"motion-{stamp}.mp4"
            video_temp_path = recordings_dir / f".motion-{stamp}.video.mp4"
            audio_temp_path = recordings_dir / f".motion-{stamp}.audio.wav"
            video_writer = cv2.VideoWriter(str(video_temp_path), cv2.VideoWriter_fourcc(*"mp4v"),
                                           args.fps, (frame.shape[1], frame.shape[0]))
            if not video_writer.isOpened():
                video_writer.release()
                camera_error = "Could not create video file"
                video_temp_path = audio_temp_path = None
            else:
                writer_queue = queue.Queue()
                buffered = list(pre_roll)
                pre_roll_seconds_for_clip = max(0, len(buffered) - 1) / float(args.fps)
                writer_thread = threading.Thread(target=write_frames,
                                                 args=(video_writer, writer_queue), daemon=True)
                writer_thread.start()
                with lock:
                    recording = True
                for buffered_frame in buffered:
                    writer_queue.put(buffered_frame)
                started = True
                audio_error = None
                try:
                    audio_process = start_audio(audio_temp_path)
                except (OSError, RuntimeError) as exc:
                    audio_process = None
                    audio_error = str(exc)
                    camera_error = audio_error

        if recording and writer_queue is not None and not started:
            writer_queue.put(frame.copy())
        if audio_process is not None and audio_process.poll() is not None:
            details = close_audio_log()
            audio_process = None
            audio_error = "Audio capture stopped unexpectedly" + (f": {details}" if details else "")
            camera_error = audio_error
        if recording and now - last_motion >= args.cooldown:
            finish_recording()


@app.route("/")
def index():
    if session.get("authenticated"):
        return redirect(url_for("home"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    ip = request.remote_addr or "unknown"
    if request.method == "POST":
        if not csrf_required():
            abort(400, "Invalid login form token")
        if login_rate_limited(ip):
            error = "Too many attempts. Wait five minutes and try again."
        elif hmac.compare_digest(request.form.get("pin", ""), args.pin):
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            with login_attempts_lock:
                login_attempts.pop(ip, None)
            return redirect(url_for("home"))
        else:
            record_login_failure(ip)
            error = "Incorrect PIN. Please try again."
    return render_template_string(LOGIN_PAGE, error=error, csrf_token=csrf_token())


@app.post("/logout")
@login_required
def logout():
    if not csrf_required():
        abort(400, "Invalid request token")
    session.clear()
    return redirect(url_for("login"))


@app.route("/home")
@login_required
def home():
    return render_template_string(HOME_PAGE, csrf_token=csrf_token())


@app.route("/live")
@login_required
def live_page():
    return render_template_string(LIVE_PAGE)


@app.route("/recordings")
@login_required
def recordings_page():
    return render_template_string(RECORDINGS_PAGE, csrf_token=csrf_token())


@app.route("/stream")
@login_required
def stream():
    def frames():
        last_sent = -1
        while True:
            with frame_condition:
                frame_condition.wait_for(lambda: frame_id != last_sent and latest_jpeg is not None,
                                         timeout=5.0)
                current_id, jpeg = frame_id, latest_jpeg
            if jpeg is None:
                continue
            last_sent = current_id
            yield b"--frame\r\nContent-Type: image/jpeg\r\nCache-Control: no-cache\r\n\r\n" + jpeg + b"\r\n"
    response = Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.route("/api/status")
@login_required
def status():
    with lock:
        return jsonify(motion=motion_active, recording=recording,
                       camera_error=camera_error, recording_file=recording_path.name if recording_path else None,
                       cooldown_seconds=args.cooldown, pre_roll_seconds=args.pre_roll)


@app.route("/api/recordings", methods=["GET"])
@login_required
def recordings_api():
    files = sorted(recordings_dir.glob("motion-*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonify([{"name": p.name, "size_mb": round(p.stat().st_size / 1024 / 1024, 2)}
                    for p in files if p.is_file()])


@app.route("/api/recordings/<path:name>", methods=["DELETE"])
@login_required
def delete_recording(name):
    if not csrf_required():
        return jsonify(error="Invalid request token; refresh the page and try again"), 400
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
@login_required
def recording_file(name):
    path = (recordings_dir / name).resolve()
    if recordings_dir not in path.parents or not path.is_file():
        return "Not found", 404
    return send_file(path, mimetype="video/mp4", as_attachment=True)


thread = threading.Thread(target=capture_loop, daemon=True)
thread.start()
app.run(host=args.host, port=args.port, threaded=True)
