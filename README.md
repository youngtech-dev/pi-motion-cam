# pi-motion-cam

Headless motion-activated USB webcam recorder for Raspberry Pi OS.

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

## Recording behavior

A five-second pre-roll is buffered in memory. Motion starts one recording, and frames are queued to a separate encoder thread so flushing pre-roll does not pause camera capture or lose the intervening footage. Repeated movement resets the same clip's cooldown; recording ends only after ten seconds with no detected visual movement. The video includes a wall-clock timestamp on every frame.

The video is no longer truncated to match the shorter audio track: audio begins at motion, is delayed by the pre-roll duration to align with the video, and video duration is preserved. If audio is unavailable, the full silent video is retained.

```bash
python3 camera.py --camera 0 --pre-roll 5 --cooldown 10 --sensitivity 25
```

## Audio

Find and test the microphone device with `arecord -l`. Use the actual card/device shown on your Pi:

```bash
arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 5 /tmp/mic-test.wav
aplay /tmp/mic-test.wav
python3 camera.py --audio-device plughw:1,0 --test-audio
```

## Run at boot

Edit `pi-motion-cam.service`, replacing `YOUR_USER` and setting the correct audio device, then install/restart:

```bash
sudo cp pi-motion-cam.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-motion-cam.service
```

Open `http://raspberrypi.local:8080/`. Completed MP4s have Download and Delete controls. The live stream is MJPEG.
