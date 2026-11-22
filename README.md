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

Motion starts recording only from visual frame differences. A recording continues writing every captured frame while idle and remains open for the full `--cooldown` period after the last motion frame. The default is 10 seconds. Further motion resets the countdown.

For example:

```bash
python3 camera.py --camera 0 --cooldown 10 --sensitivity 25
```

The `RECORDING` status remains true for the cooldown. `--sensitivity` controls what counts as visual motion; lower values are more sensitive.

## Run at boot

```bash
sudo cp pi-motion-cam.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-motion-cam.service
```

Open `http://raspberrypi.local:8080/`. Completed MP4s have Download and Delete controls. The live stream is MJPEG and saved recordings combine webcam video and microphone audio when ALSA works.
