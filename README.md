# pi-motion-cam

Headless motion-activated USB webcam recorder for Raspberry Pi OS. Saves MP4 video with the webcam microphone, serves an MJPEG live view, and allows completed recordings to be downloaded or deleted. Motion detection is video-only; sound never triggers recording.

## Install

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv v4l-utils ffmpeg alsa-utils
mkdir -p ~/pi-motion-cam
cd ~/pi-motion-cam
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Diagnose the microphone first

The camera and its microphone are separate Linux devices. `plughw:1,0` is only an example: USB card numbers can change, and the actual webcam mic might not be card 1. List capture devices:

```bash
arecord -l
arecord -L
```

Test the exact capture device reported by the Pi (replace `plughw:1,0` as needed):

```bash
arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 5 /tmp/mic-test.wav
aplay /tmp/mic-test.wav
```

Then use the same device with the app's built-in FFmpeg test:

```bash
.venv/bin/python camera.py --audio-device plughw:1,0 --test-audio
```

This test reports FFmpeg/ALSA errors directly and exits without starting the camera server. If `arecord` succeeds but this test fails, check `ffmpeg` installation and device busy/permissions. If running under systemd, make sure the service uses the same device and user permissions; after editing the unit, reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart pi-motion-cam.service
sudo journalctl -u pi-motion-cam.service -n 100 --no-pager
```

The web status displays the configured audio device and any current capture/finalization error. Audio capture is separate from visual motion detection.

## Run at boot

Edit `pi-motion-cam.service`: replace `YOUR_USER`, check the working directory, and set `--audio-device` to the actual ALSA device tested above. Then install/restart:

```bash
sudo cp pi-motion-cam.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-motion-cam.service
```

The service includes the `audio` supplementary group. If the service user still receives permission errors, verify `arecord` works as that user and check the system journal.

## Run manually

```bash
.venv/bin/python camera.py --camera 0 --audio-device plughw:1,0 --port 8080
```

Open `http://raspberrypi.local:8080/` from another device on the network. The app listens on port 8080 by default.

## Behavior and settings

The app starts recording on detected visual motion and continues writing frames until `--cooldown` seconds after the last detected visual motion (10 seconds by default). Additional visual motion resets the timer. `--sensitivity` controls the changed-pixel percentage threshold. Saved MP4 audio is captured through FFmpeg/ALSA and muxed when recording finishes; the live MJPEG stream is video-only. Use `--no-audio` to disable audio.

Available options include `--camera`, `--host`, `--port`, `--width`, `--height`, `--fps`, `--sensitivity`, `--cooldown`, `--recordings`, `--audio-device`, `--audio-rate`, `--no-audio`, and `--test-audio`.

## Manage recordings and security

The web page lists completed recordings with Download and Delete buttons. Delete prompts for confirmation; recordings currently being written cannot be deleted. Deletion is permanent. The web server currently has no authentication, so keep it on a trusted network or use a firewall/VPN before exposing it beyond your LAN.
