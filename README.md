> **This is a fork of [denvera/bt-proxy](https://github.com/denvera/bt-proxy).** It measures the scanner's liveness from advertisements actually arriving rather than from BlueZ accepting the discovery call, so a controller that wedges after a successful start is torn down and reported failed instead of being listed as a healthy proxy; and it never forwards BlueZ's -127 no-RSSI sentinel as a real reading, carrying the last real value forward briefly and otherwise dropping the advertisement, so nothing downstream computes a distance from a signal that was never measured.
> Read branch **`scanner-liveness`**; `main` mirrors upstream unchanged.
> Changes meant for upstream are prepared as patch series against `main`.
>
> **The README below is upstream's**, with this fork's changes folded in where they apply.
> Its install and image instructions point at upstream and will not give you the changes
> above. To run this fork: `git clone -b scanner-liveness https://github.com/pete-jorgensen/bt-proxy.git`.
# bt-proxy

ESPHome-compatible Bluetooth Proxy for Raspberry Pi.

This implements the ESPHome [Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy/) functionality in Python, allowing a Raspberry Pi to act as a BLE proxy for Home Assistant. It speaks the ESPHome Native API protocol so Home Assistant discovers and uses it exactly like an ESP32-based Bluetooth proxy.

WARNING: This project was coded largely with the assistance of an LLM. It works for me, but your mileage may vary.

## Features

- **BLE scanning** — passive and active scan modes, raw advertisement forwarding
- **Active connections** — GATT connect/disconnect, service discovery, read/write characteristics and descriptors, notifications
- **mDNS discovery** — automatically advertised so Home Assistant finds it
- **ESPHome Native API** — wire-compatible with `aioesphomeapi` / Home Assistant ESPHome integration

## Requirements

- Raspberry Pi (or any Linux machine) with a Bluetooth adapter
- [uv](https://docs.astral.sh/uv/) package manager
- BlueZ (installed by default on Raspberry Pi OS)

## Installation

```bash
git clone https://github.com/denvera/bt-proxy.git /opt/bt-proxy
cd /opt/bt-proxy
uv sync
```

## Usage

```bash
uv run python -m bt_proxy
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--name` | `bt-proxy` | Device name (used in mDNS and API) |
| `--friendly-name` | `Bluetooth Proxy` | Human-readable name |
| `--port` | `6053` | API server TCP port |
| `--max-connections` | `3` | Max concurrent BLE GATT connections |
| `--adapter` | system default | Bluetooth adapter (e.g. `hci0`) |
| `--advert-timeout` | `300` | Seconds of advertisement silence that count as a dead scanner. `0` disables the watchdog |
| `--log-level` | `INFO` | Logging verbosity |

### Example

```bash
uv run python -m bt_proxy --name living-room-proxy --friendly-name "Living Room BT Proxy" --log-level DEBUG
```

### Scanner liveness

A controller can accept `StartDiscovery` and then deliver nothing — a firmware handshake
that times out leaves BlueZ reporting a healthy adapter that never produces an
advertisement. Trusting the start call therefore makes a dead radio indistinguishable from
a quiet one: the proxy keeps reporting as running and Home Assistant goes on counting it
among its Bluetooth sources.

Liveness is measured from advertisements arriving instead. Every detection stamps a
monotonic timestamp; a watchdog checks every 30 s and, after `--advert-timeout` seconds of
silence while the scanner is supposed to be running, tears the scanner down and reports
`SCANNER_STATE_FAILED` so the client stops trusting this proxy, then hands over to the
existing re-arm loop. The timestamp is stamped before the no-client early return, so a
proxy nobody has connected to still counts as receiving, and a deliberate stop — shutdown,
or the stop/start inside a scan-mode change — cancels the watchdog so a mode switch cannot
look like a dead radio.

Set `--advert-timeout 0` only where no BLE device is expected to advertise within the
window; on a site with any traffic at all it disables the one check that notices this
failure.

### A started scanner is not a working radio

Two independent ways the proxy can look healthy while receiving nothing, and the check
for each:

**The controller accepts and then delivers nothing.** Covered by the advertisement
watchdog above.

**The adapter is not powered at all.** BlueZ accepts a passive-scan request against an
unpowered controller and then refuses to register the advertisement monitor —
`Failed to Add Adv Patterns Monitor with status 0x0a` (MGMT_STATUS_NOT_POWERED) — and that
refusal never reaches the client. bleak's `start()` returns cleanly. So the adapter's
`Powered` property is read over D-Bus after a successful start, and the proxy reports
`SCANNER_STATE_FAILED` rather than `RUNNING` when it is false. The query is best-effort:
if it cannot be answered (no dbus-fast, no D-Bus, a timeout) the result is "cannot tell"
and never blocks a scanner that is otherwise working.

Without this, a proxy whose controller has died reports as running indefinitely, and Home
Assistant goes on routing connections to it.

**A wedged UART-attached controller recovers when its serdev driver is re-probed**:
unbind it, then bind it. No delay is needed between the two writes. Powering the adapter back on through
rfkill, `bluetoothctl` or `hciconfig` does not recover this state. `<dev>` is the serdev
device and `<drv>` its driver under `/sys/bus/serial/drivers/`:

```bash
echo <dev> | sudo tee /sys/bus/serial/drivers/<drv>/unbind
echo <dev> | sudo tee /sys/bus/serial/drivers/<drv>/bind
```

### Connection ownership

Every GATT link exists because some client asked for it, and it is released when that
client goes away. Without that, a link can outlive the request that created it — and a
connected BLE peripheral stops advertising, so a stranded link does not merely occupy a
slot: it makes the device undiscoverable to every proxy, including the one holding it.
The failure is self-perpetuating, because the run that strands the link is the same kind of
run that then cannot find the device.

Four rules keep it from happening:

- **A connect is bounded.** The retry loop stops at `CONNECT_TOTAL_TIMEOUT` (60 s) rather
  than running to `CONNECT_MAX_RETRIES * CONNECT_TIMEOUT` plus delays, which is ~94 s —
  longer than any client waits.
- **A link that arrives too late is dropped.** If the requester cancelled while the connect
  was in flight, or the connection was already retired, the established link is disconnected
  before returning rather than kept. A disconnect arriving mid-retry also stops the loop
  instead of letting it complete.
- **A link with no owner is swept.** Losing a client releases its links immediately; a
  sweeper every `ORPHAN_SWEEP_INTERVAL` (30 s) is the backstop for anything that misses.

- **A link this process asked for and no longer tracks is released.** The three rules
  above all read the proxy's own bookkeeping, so none of them can see the one link that
  bookkeeping has already let go of: a connect that times out here is forgotten here, while
  BlueZ may still complete it. Each sweep therefore also asks BlueZ what is really
  connected, and drops any link that (a) this process itself once tried to connect,
  (b) it has no entry for and is not currently connecting, and (c) was in that state on the
  previous sweep too. A link the proxy never asked for — a bonded phone, another daemon on
  the same adapter — is never touched, and if BlueZ cannot be asked nothing is released.
  This case is worth the extra sweep: a connect that gives up and is forgotten here can
  still complete in BlueZ, and the peripheral then stays connected — and silent to every
  proxy — until something releases the link. An untracked link cannot be confirmed from
  the outside, which is why the sweep asks BlueZ rather than inferring.

The sweeper keys on **ownership, not idleness**. A healthy link behind a notify subscription
can sit silent for hours, so an idle timer would disconnect working connections; an owner
that has gone away is a fact rather than a guess. Where liveness cannot be determined at
all, nothing is swept.

Diagnosing a stranded link on the host, if one is ever suspected:

```bash
bluetoothctl info <MAC>        # Connected: yes, while no scan can see the device
bluetoothctl disconnect <MAC>  # it advertises again within ~3 s
```

### RSSI

BlueZ does not always attach an RSSI. In passive mode an advertisement can arrive with no
RSSI property, and bleak fills in `-127`. That value is also a legal, very weak reading, so
forwarding it is not a null: Home Assistant stores it as this scanner's signal for the
device, and anything computing range from it — distance and area sensors, Home Assistant's
own choice of nearest proxy — concludes the device is far away, with nothing looking like
an error anywhere.

The reported value is used when it is real; otherwise this address's last real reading is
carried forward while it is under 60 s old; otherwise the advertisement is dropped. Dropping
loses the payload, which is a real cost for a device that only ever reports without an RSSI,
but an advertisement that cannot be positioned is of little use to a proxy and a gap beats a
confident wrong position. Drops are counted and logged every 500 so the cost is visible.

## How It Works

1. Starts a BLE scanner using [bleak](https://github.com/hbldh/bleak)
2. Advertises itself via mDNS as `_esphomelib._tcp.local.`
3. Listens on TCP port 6053 for ESPHome Native API connections
4. When Home Assistant connects, it forwards BLE advertisements and handles GATT operations

> **Note:** This uses the ESPHome Native API **plaintext** variant (no encryption). The Noise-encrypted protocol is currently not supported.

## Running with Docker

Pre-built images are available for `amd64`, `arm64`, and `arm/v7` (Raspberry Pi 2+):

```bash
docker run -d \
  --name bt-proxy \
  --restart unless-stopped \
  --net=host \
  --privileged \
  -v /var/run/dbus:/var/run/dbus \
  ghcr.io/denvera/bt-proxy
```

Pass any CLI options after the image name:

```bash
docker run -d \
  --name bt-proxy \
  --restart unless-stopped \
  --net=host \
  --privileged \
  -v /var/run/dbus:/var/run/dbus \
  ghcr.io/denvera/bt-proxy \
  --name living-room-proxy --friendly-name "Living Room BT Proxy" --log-level DEBUG
```

> `--net=host` is required for mDNS discovery. `--privileged` grants access to the Bluetooth adapter — alternatively use `--cap-add=NET_ADMIN --cap-add=NET_RAW` with explicit device mounts.

## Running as a Service

The unit runs the virtualenv that `uv sync` creates in `/opt/bt-proxy/.venv`, so
`uv` doesn't need to be on root's `PATH`. If you cloned somewhere else, adjust
`WorkingDirectory` and `ExecStart` to match. After pulling updates, re-run
`uv sync` and restart the service.

Copy the unit file and enable it:

```bash
sudo cp bt-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bt-proxy
```

Check status / logs:

```bash
sudo systemctl status bt-proxy
journalctl -u bt-proxy -f
```

## Scanning modes: active vs passive

Home Assistant requests **passive** scanning by default. bt-proxy honours
that when BlueZ supports it, and otherwise falls back to **active** scanning
automatically — so things keep working either way. If passive isn't available
you'll see a one-off warning in the log followed by `falling back to active
scanning`; that's harmless.

Passive is generally the better default. It's lighter-weight on the proxy itself
(the radio only listens, never transmits, which also reduces RF congestion when
several proxies are around), and it can be gentler on some battery sensors:

- **Active** scanning may send a scan request to a *scannable* advertiser,
  prompting it to transmit an extra scan-response packet. **Passive** never asks
  for one.
- For non-connectable beacons (they can't be scan-requested), and for devices the proxy
  stays connected to — once connected, the connection dominates a device's power
  use, not the scan mode. It mainly matters for advertisement-only sensors that
  happen to be scannable.

### Enabling passive scanning

Passive scanning needs BlueZ's experimental features (and **BlueZ ≥ 5.56** with
**Linux kernel ≥ 5.10**, which most current systems already have). Run
`bluetoothd` with `--experimental`.

Rather than editing the packaged unit, drop in an override so updates don't
clobber it. Create `/etc/systemd/system/bluetooth.service.d/experimental.conf`:

```ini
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd --experimental
```

The empty `ExecStart=` is required to clear the inherited command before
setting the new one. Then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart bluetooth
```

> Match the `bluetoothd` path to your system — some distros use
> `/usr/lib/bluetooth/bluetoothd`. Check with `systemctl cat bluetooth | grep ExecStart`.

Once enabled, the proxy logs `Starting BLE scanner (configured=passive,
mode=passive)` and the active-fallback warning goes away.

## Reliability on Raspberry Pi

Undervolting or brownout/undervoltage conditions on Pi's produce exactly the 
symptoms that look like flaky bluetooth, often manifested as HCI command timeouts (`dmesg`: `hci0: Opcode 0x200c failed`,
`tx timeout`), `Frame reassembly failed`, dropped connections, even an unresponsive `bluetoothd`.

**Before blaming bluetooth, check power:**

```bash
vcgencmd get_throttled      # want 0x0 (or at most 0x50000 = a brief boot-time
                            # dip); a non-zero bit 0 means under-voltage NOW
```

Use a proper **5.1 V / ≥2.5 A** supply and a short, thick cable. Undervoltage
under load (`0x...1` / `0x...5`) is the most common cause of "random" BT
failures on a Pi.

The proxy also **backs off** instead of hammering the adapter when connections
keep failing, which both protects a flaky controller and keeps the logs
readable.

### Adapter powered off / blocked by rfkill (common on a fresh install)

If the proxy logs `the Bluetooth adapter is powered off or blocked` /
`No powered Bluetooth adapters found ... POWERED_OFF`, the adapter simply isn't
powered — it's **not** a wedged controller. On a fresh Raspberry Pi OS install
the onboard Bluetooth is often **soft-blocked by rfkill**. Unblock it **on the
host**:

```bash
sudo rfkill unblock bluetooth
sudo hciconfig hci0 up          # only if it's still down afterwards
```

This is a host-level fix — even a `--privileged` container can't power on an
rfkill-blocked host adapter. The proxy detects this case and keeps retrying
(every 60s), so once you unblock it the scanner starts on its own without a
restart.

> `rfkill` may not be installed by default (`sudo apt install rfkill`). The
> unblock is saved across reboots (via `systemd-rfkill`), so it's usually a
> one-time fix.

### When bluetooth appears unresponsive

Two distinct failures can occur on the onboard radio, with different fixes:

- **Controller unresponsive** — `dmesg` shows `hci0: Opcode 0x200c failed: -110/-16`.
  Restarting `bluetoothd` is *not* enough; reset the adapter:
  ```bash
  sudo hciconfig hci0 reset    # or: down && up, or reboot
  ```
- **Daemon unresponsive** — the proxy logs that it looks like *"bluetoothd is wedged
  (phantom connection / no D-Bus reply)"*. Here the **daemon** needs a restart
  (an adapter reset alone won't fix it, and neither will restarting bt-proxy):
  ```bash
  sudo systemctl restart bluetooth
  ```

bt-proxy tries to detect these conditions, log a recovery hint, and back off so it
recovers once the underlying issue clears — but it wont 
reset the adapter or restart `bluetoothd` itself (those are host-level actions).

### Optional: auto-recover an unresponsive `bluetoothd` (watchdog)

If you want an automatic, albeit somewhat primitive  recovery from the unresponsive daemon
 case, add a small systemd watchdog on the **host** that restarts `bluetooth` when the proxy reports it's
frozen. For example, a timer that restarts the daemon when the marker shows up
in the proxy's logs:

```ini
# /etc/systemd/system/bt-proxy-watchdog.service
[Unit]
Description=Restart bluetoothd if unresponsive

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'journalctl -u bt-proxy --since "-3min" | grep -q "bluetoothd is wedged" && systemctl restart bluetooth || true'
```

```ini
# /etc/systemd/system/bt-proxy-watchdog.timer
[Unit]
Description=Periodically check whether bluetoothd needs restarting

[Timer]
OnUnitActiveSec=2min
AccuracySec=30s

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bt-proxy-watchdog.timer
```

> Adjust the `journalctl -u` unit name (and add `-t`/container filters) to match
> how you run the proxy. Under Docker, point it at the container logs instead,
> e.g. `docker logs --since 3m bt-proxy 2>&1 | grep -q ...`.

## Architecture

```
bt_proxy/
├── __init__.py        # Package init
├── __main__.py        # Entry point, CLI, mDNS registration
├── proto.py           # Protobuf encoding/decoding, message IDs, wire protocol
├── ble_manager.py     # BLE scanning and GATT connections (bleak)
└── api_server.py      # ESPHome Native API TCP server

tests/                 # pytest; a fake bleak client, no radio needed
```

## Tests

```bash
pip install -e '.[test]'
pytest
```

The suite covers the connection lifecycle — who owns a link, and what happens to one that
arrives after its requester has gone. Those are races, so the fake client can be held
mid-connect rather than timed with sleeps.
