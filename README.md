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

A motion event starts a recording and a separate 5-second pre-roll buffer is prepended by default. While the clip is open, every captured frame is written continuously. Each detected visual motion resets `--cooldown`; the clip therefore remains open across repeated movements and ends only when `--cooldown` seconds pass with no visual movement.

Recordings include a top-left wall-clock timestamp with tenths of a second.

```bash
python3 camera.py --camera 0 --pre-roll 5 --cooldown 10 --sensitivity 25
```

The default `--cooldown` is 10 seconds. If actual timing differs due to capture-rate/load, increase only if needed. Do not add a separate large systemd cooldown: the application uses real elapsed monotonic time and is no longer layered with UI's 1-second motion state.

## Audio

Find the microphone device with `arecord -l`, test it, and pass the exact ALSA capture name:

```bash
arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 5 /tmp/mic-test.wav
aplay /tmp/mic-test.wav
python3 camera.py --audio-device plughw:1,0 --test-audio
```

The service must use the same device. Update `pi-motion-cam.service` and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart pi-motion-cam.service
```

## Run at boot

Edit `pi-motion-cam.service`, replacing `YOUR_USER`, and set the webcam and microphone options. Then:

```bash
sudo cp pi-motion-cam.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-motion-cam.service
```

Open `http://raspberrypi.local:8080/`. Completed MP4s have Download and Delete controls. The live stream is MJPEG; saved recordings combine video and webcam audio when ALSA works.
