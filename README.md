# pi-motion-cam

A headless motion-activated USB webcam recorder for Raspberry Pi OS. It records to MP4, provides an MJPEG live stream, and lists/downloads recordings through a small Flask web interface.

## Hardware

- Raspberry Pi 3B or newer
- Linux-compatible USB webcam
- Optional: 32 GB or larger high-quality microSD card

## Install on Raspberry Pi OS (64-bit or 32-bit)

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv v4l-utils
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

Test the webcam:

```bash
v4l2-ctl --list-devices
python3 camera.py --camera 0 --port 8080
```

Open `http://raspberrypi.local:8080/` from another machine. Stop with `Ctrl+C`.

## Run at boot

1. Replace `YOUR_USER` in `pi-motion-cam.service`.
2. Copy the service file and enable it:

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

The service starts `/usr/bin/python3` with the application environment. Override defaults in `camera.py` using command-line options:

```text
--camera       OpenCV camera index (default: 0)
--host         Listen address (default: 0.0.0.0)
--port         HTTP port (default: 8080)
--width        Capture width (default: 640)
--height       Capture height (default: 480)
--fps          Target capture FPS (default: 15)
--sensitivity  Percentage of changed pixels to trigger motion (default: 25)
--cooldown     Seconds recording continues after motion stops (default: 10)
--recordings   Recording directory (default: ./recordings)
```

Example: `python3 camera.py --camera 0 --sensitivity 15 --cooldown 20`

## Notes

- MP4 files use the `mp4v` codec and can be opened in VLC or modern browsers.
- The live endpoint is `http://HOST:8080/stream`; it is an MJPEG stream.
- Use a firewall/VPN or an encrypted reverse proxy before exposing the server to the internet.
- To add disk cleanup, monitor usage with `df -h` and remove old files from `~/pi-motion-cam/recordings`.
