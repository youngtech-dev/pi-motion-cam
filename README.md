# pi-motion-cam

A headless motion-activated USB webcam recorder for Raspberry Pi OS. It records MP4 video with webcam audio, provides an MJPEG live stream, and lists, downloads, and deletes recordings in a small Flask web interface. Motion detection uses video frames only; microphone audio never triggers a recording.

## Hardware and install

- Raspberry Pi 3B or newer and a Linux-compatible USB webcam with microphone.

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv v4l-utils ffmpeg alsa-utils
mkdir -p ~/pi-motion-cam
cd ~/pi-motion-cam
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Raspberry Pi OS 32-bit, if the OpenCV pip package is unavailable, install `python3-opencv` with apt and then `pip install Flask numpy`.

## Find and test the webcam microphone

The USB webcam microphone is a separate ALSA capture device; it is not automatically selected just because the camera is selected. Find its card/device numbers:

```bash
arecord -l
```

For example, `card 1, device 0` is usually addressed as `plughw:1,0`. `plughw` is preferable to `hw` because it allows ALSA to convert supported rates and channel counts. Test the device before starting the service:

```bash
arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 5 /tmp/mic-test.wav
aplay /tmp/mic-test.wav
```

Run the app with that same device:

```bash
python3 camera.py --camera 0 --audio-device plughw:1,0 --port 8080
```

Use the actual card/device numbers reported on your Pi. If `arecord` reports permission denied while running as a service, add that service user to the `audio` group (`sudo usermod -aG audio YOUR_USER`) and restart the service/login. Inspect errors via `/api/status` or `sudo journalctl -u pi-motion-cam.service -f`. Audio is recorded independently through FFmpeg/ALSA; audio levels do not participate in motion detection.

Open `http://raspberrypi.local:8080/` from another machine. Stop a foreground run with `Ctrl+C`.

## Run at boot

Edit `pi-motion-cam.service`: replace `YOUR_USER` and set `--audio-device` to the tested ALSA device (for example `plughw:1,0`). Then:

```bash
sudo cp pi-motion-cam.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-motion-cam.service
sudo systemctl status pi-motion-cam.service
sudo journalctl -u pi-motion-cam.service -f
```

## Configuration

`camera.py` supports `--camera`, `--host`, `--port`, `--width`, `--height`, `--fps`, `--sensitivity`, `--cooldown`, `--recordings`, `--audio-device`, and `--no-audio`. The default cooldown is 10 seconds and recording continues writing every video frame until ten seconds after the last detected visual motion. Further motion resets that timer.

## Web interface and security

The home page shows the live video and completed recordings. Use Download or Delete beside a recording; deletion asks for confirmation and is permanent. A recording in progress cannot be deleted. The live `/stream` endpoint is video-only MJPEG; saved MP4 recordings include audio when the ALSA device is available. If audio cannot start, the app continues recording silent video and reports the cause in `/api/status`.

Use a firewall/VPN or an encrypted reverse proxy before exposing this server beyond a trusted network. Monitor disk usage and remove recordings when needed.
