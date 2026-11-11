# Linux PS4 Remote Play

On Linux, the MCP server uses the pinned Chiaki-ng desktop client for the PS4
Remote Play connection. The existing Swift/AppKit viewer remains the macOS
backend. Linux sends MCP snapshots from Chiaki-ng's Xwayland window and uses
Chiaki-ng's keyboard-as-controller mapping for button and stick input.

## Build and run

On Debian 13, install the native build dependencies:

```sh
sudo apt install build-essential cmake ninja-build pkg-config \
  qt6-base-dev qt6-base-private-dev qt6-declarative-dev qt6-svg-dev \
  libplacebo-dev libavcodec-dev libavformat-dev libavutil-dev libswscale-dev \
  libopus-dev libssl-dev libsdl2-dev libevdev-dev libudev-dev \
  libcurl4-openssl-dev libjson-c-dev libminiupnpc-dev libevent-dev \
  libprotobuf-c-dev protobuf-c-compiler python3-venv xdotool netpbm
make linux-chiaki
python3 -m venv .venv
.venv/bin/pip install -e .
```

Pair the PS4 in Chiaki-ng once. Enable **Keyboard as controller** in Chiaki-ng
settings. Keep the Remote Play session open. If Chiaki-ng is not already
running, the MCP backend launches `build/linux-chiaki/gui/chiaki`; set
`PS5MCP_CHIAKI` to use a different binary.

Run the MCP server from a graphical Linux login session so it can use that
session's Xwayland socket and Xauthority file:

```sh
PS4_HOST=10.168.2.52 .venv/bin/ps5mcp-server
```

If the shell did not inherit `DISPLAY`, the backend detects this user's GNOME
Xwayland display and authorization file. It starts Chiaki-ng through Xwayland so
the window can be captured and controlled consistently on X11 and Wayland.

The initial Linux backend supports snapshots, button presses, holds, sequences,
and digital stick directions through Chiaki-ng's default keyboard map. PS5
payload commands, PS4 title management, text-entry automation, continuous
analog stick magnitude, and touchpad gestures still need Linux/PS4-native
implementations. Chiaki-ng manages and stores the Remote Play registration.
