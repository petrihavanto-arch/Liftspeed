# Liftspeed

Liftspeed samples a VL53L1X distance sensor and provides a Flask web UI for recording and analysing lifting reps.

## Run the web app

Create a virtual environment and install the project (on a Raspberry Pi, include the optional sensor dependency):

```sh
uv sync --extra rpi
uv run python webapp.py --host 0.0.0.0 --port 5000
```

Open `http://<device-address>:5000`. Use `--platform pc` to test without the sensor hardware.

## Run as a systemd service and access over Tailscale

The included `liftspeed.service` runs the web app at boot on port 5000. It follows the `lamp.service` deployment pattern; adjust `User`, `WorkingDirectory`, and `ExecStart` in the unit if your account or checkout path differs from `/home/pi/python/Liftspeed`.

On the device, install the project in that checkout first:

```sh
cd /home/pi/python/Liftspeed
uv sync --extra rpi
sudo cp liftspeed.service /etc/systemd/system/liftspeed.service
sudo systemctl daemon-reload
sudo systemctl enable --now liftspeed.service
```

Ensure Tailscale is installed, connected, and running on the device and on the client. Find the device's Tailscale IPv4 address with:

```sh
tailscale ip -4
```

Then open `http://<tailscale-ip>:5000` from a device on your tailnet. Tailscale ACLs must permit the client to reach TCP port 5000 on this device. The service binds to `0.0.0.0`, so it also listens on other network interfaces; use host firewall rules if access should be limited to Tailscale.

To inspect the service logs or stop the service:

```sh
sudo journalctl -u liftspeed.service -f
sudo systemctl disable --now liftspeed.service
```

**Security note:** the web app uses Flask's built-in development server and is documented as not being a production/multi-client deployment. Tailscale controls network reachability but does not change that limitation; use only on a trusted tailnet and do not expose the port publicly.
