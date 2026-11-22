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

## Audio

Find and test the webcam microphone:

```bash
arecord -l
arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 5 /tmp/mic-test.wav
aplay /tmp/mic-test.wav
python3 camera.py --audio-device plughw:1,0 --test-audio
```

The card number is hardware-specific and may change after a reboot. Use the exact `arecord -l` result. The systemd service includes the `audio` group. Reload and restart after changing it:

```bash
sudo systemctl daemon-reload
sudo systemctl restart pi-motion-cam.service
```

## Recording behavior

Motion starts recording only from visual frame differences. A rolling video buffer is maintained before detection and prepended to a new clip, so the first moments are captured even though motion detection needs a frame or two to recognize movement. The default pre-roll is 3 seconds (`--pre-roll`); increase it if needed. The video continues writing through idle frames until 10 seconds (`--cooldown`) after the last visual motion. Further visual motion resets the cooldown. The pre-roll applies to video; the audio track starts at detection.

```bash
python3 camera.py --camera 0 --pre-roll 3 --cooldown 10 --sensitivity 25
```

The pre-roll ring buffer uses RAM (about 40 MB at 640x480, 15 FPS, and 3 seconds). `--sensitivity` controls what counts as motion; lower values are more sensitive.

## Run at boot

Edit `pi-motion-cam.service`, replacing `YOUR_USER` and setting the correct audio device. Then:

```bash
sudo cp pi-motion-cam.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-motion-cam.service
```

If you change options, edit the unit file, then run `sudo systemctl daemon-reload && sudo systemctl restart pi-motion-cam.service`.

Open `http://raspberrypi.local:8080/`. Completed MP4s have Download and Delete controls. The live stream is MJPEG; saved recordings combine webcam video and microphone audio when ALSA works.
