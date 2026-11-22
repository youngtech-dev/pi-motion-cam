# pi-motion-cam

Headless motion-activated USB webcam recorder and lightweight web interface for Raspberry Pi OS. The site uses a four-digit PIN login, then offers separate live-stream and recordings pages. Recordings are refreshed only when the user requests it.

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

## Configure PIN and session key

Set a private four-digit PIN and a long random Flask session key in a root-readable environment file. Do not commit these secrets:

```bash
sudo sh -c 'umask 077; printf "PI_CAM_PIN=YOUR_4_DIGIT_PIN\\nPI_CAM_SECRET_KEY=%s\\n" "$(python3 -c \'import secrets; print(secrets.token_hex(32))\')" > /etc/pi-motion-cam.env'
sudo chmod 600 /etc/pi-motion-cam.env
```

The systemd unit reads `/etc/pi-motion-cam.env`. For a foreground run, export the variables before launching:

```bash
export PI_CAM_PIN=YOUR_4_DIGIT_PIN
export PI_CAM_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
.venv/bin/python camera.py
```

## Microphone

Find and test the microphone device with `arecord -l`. Use the actual card/device shown on your Pi:

```bash
arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 5 /tmp/mic-test.wav
aplay /tmp/mic-test.wav
.venv/bin/python camera.py --audio-device plughw:1,0 --test-audio
```

## Recording behavior

A five-second pre-roll is buffered in memory. Motion starts one recording, and frames continue to be recorded until 40 seconds by default after the last detected visual movement. Repeated movement resets the same clip's cooldown. Each frame has a wall-clock timestamp. Audio is muxed into the saved MP4 when the ALSA device is available.

## Web interface

Visit `http://raspberrypi.local:8080/` and enter the PIN. The dashboard links to Live Stream and Recordings. The live page refreshes its status while open and serves each newly captured JPEG once for a smoother MJPEG stream. The recordings page does not poll; click **Refresh recordings** when you want an updated list. Downloads and deletion require login; delete asks for confirmation.

The four-digit PIN is a convenience gate, not strong protection against a determined attacker. Keep the app on a trusted LAN or use a VPN/HTTPS reverse proxy for remote access. The Flask session key should remain secret.

## Run at boot

```bash
sudo cp pi-motion-cam.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-motion-cam.service
sudo systemctl status pi-motion-cam.service
```

After changing the service or `/etc/pi-motion-cam.env`, restart with `sudo systemctl daemon-reload && sudo systemctl restart pi-motion-cam.service`.
