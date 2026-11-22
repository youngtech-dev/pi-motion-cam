# pi-motion-cam

A headless motion-activated USB webcam recorder for Raspberry Pi OS. It records MP4 video with audio, provides an MJPEG live stream, and lists/downloads recordings through a small Flask web interface. Motion detection uses video frames only; microphone audio is recorded but never triggers a recording.

## Hardware

- Raspberry Pi 3B or newer
- Linux-compatible USB webcam (including a webcam microphone)
- Optional: 32 GB or larger high-quality microSD card

## Install on Raspberry Pi OS (64-bit or 32-bit)

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv v4l-utils ffmpeg alsa-utils
mkdir -p ~/pi-motion-cam
cd ~/pi-motion-cam
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Raspberry Pi OS 32-bit, install `python3-opencv` from apt instead if the package listed below is unavailable:

```bash
sudo apt install -y python3-opencv
pip install Flask numpy
```

Identify the camera and its microphone:

```bash
v4l2-ctl --list-devices
arecord -l
```

The USB webcam microphone appears as an ALSA capture device. Use `hw:CARD,DEVICE` from `arecord -l` (for example `hw:1,0`) if the system's `default` input is not the webcam. Test the microphone first with:

```bash
arecord -D hw:1,0 -f S16_LE -r 16000 -c 1 -d 5 /tmp/mic-test.wav
```

Start the app, selecting the appropriate ALSA device:

```bash
python3 camera.py --camera 0 --audio-device hw:1,0 --port 8080
```

Open `http://raspberrypi.local:8080/` from another machine. Stop with `Ctrl+C`.

## Run at boot

1. Replace `YOUR_USER` in `pi-motion-cam.service`.
2. Edit the service's `--audio-device` argument if the webcam microphone is not available through ALSA's `default` device.
3. Copy the service file and enable it:

```bash
sudo cp pi-motion-cam.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-motion-cam.service
sudo systemctl status pi-motion-cam.service
```

View output:

```bash
sudo journalctl -u pi-motion-cam.service -f
```

## Configuration

The service starts the application with command-line options:

```text
--camera       OpenCV camera index (default: 0)
--host         Listen address (default: 0.0.0.0)
--port         HTTP port (default: 8080)
--width        Capture width (default: 640)
--height       Capture height (default: 480)
--fps          Target capture FPS (default: 15)
--sensitivity  Percentage of changed video pixels to trigger motion (default: 25)
--cooldown     Seconds recording continues after visual motion stops (default: 10)
--recordings   Recording directory (default: ./recordings)
--audio-device ALSA input for FFmpeg (default: default)
--no-audio     Disable audio recording
```

Example: `python3 camera.py --camera 0 --audio-device hw:1,0 --sensitivity 15 --cooldown 20`

## Notes

- Audio is captured separately by FFmpeg and muxed into each MP4 after recording. Install `ffmpeg` and ensure the service user can access the ALSA capture device.
- Only visual frame changes trigger recordings. Audio levels are not used in motion detection.
- The live endpoint `http://HOST:8080/stream` is video-only MJPEG; audio is included in saved recordings.
- If audio capture cannot start, the app continues saving silent video and reports the error in `/api/status`.
- Use a firewall/VPN or an encrypted reverse proxy before exposing the server to the internet.
- To add disk cleanup, monitor usage with `df -h` and remove old files from the recordings directory.
