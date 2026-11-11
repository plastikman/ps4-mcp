"""Linux adapter for a Chiaki-ng desktop session.

The upstream macOS viewer is AppKit/AVFoundation-only. On Linux this adapter
uses Chiaki-ng for the Remote Play transport and its built-in keyboard
controller, with Xwayland for frame capture and input injection.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import capture
from .protocol import CENTER, PadState


class LinuxChiakiError(capture.CaptureError):
    pass


BUTTON_KEYS = {
    0x000002: "5",             # L3
    0x000004: "6",             # R3
    0x000008: "o",             # Options
    0x000010: "Left",
    0x000020: "Right",
    0x000040: "Down",
    0x000080: "Up",
    0x000100: "1",             # L2
    0x000200: "4",             # R2
    0x000400: "2",             # L1
    0x000800: "3",             # R1
    0x001000: "c",             # Triangle
    0x002000: "BackSpace",     # Circle
    0x004000: "Return",        # Cross
    0x008000: "backslash",     # Square
    0x010000: "Escape",        # PS
    0x100000: "t",             # Touchpad click
}

AXIS_KEYS = {
    "lx": ("bracketleft", "bracketright"),
    "ly": ("Insert", "Delete"),
    "rx": ("minus", "equal"),
    "ry": ("Page_Up", "Page_Down"),
}


def _display_environment() -> dict[str, str]:
    """Find this user's Xwayland endpoint when the agent shell lost session vars."""
    env = os.environ.copy()
    runtime = Path(env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    # Agent-launched shells may not inherit DISPLAY/XAUTHORITY from GNOME.
    # Mutter's auth file is a reliable sign that the standard Xwayland display
    # is available, and avoids looking in /tmp for its socket.
    if not env.get("DISPLAY") and any(runtime.glob(".mutter-Xwaylandauth.*")):
        env["DISPLAY"] = ":0"
    if not env.get("XAUTHORITY"):
        auth_files = sorted(runtime.glob(".mutter-Xwaylandauth.*"))
        if auth_files:
            env["XAUTHORITY"] = str(auth_files[-1])
    env.setdefault("QT_QPA_PLATFORM", "xcb")
    return env


def _run(command: list[str], *, env: dict[str, str], timeout: float = 5) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, env=env, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise LinuxChiakiError(f"Missing Linux desktop tool {command[0]!r}; install xdotool and netpbm.") from exc
    except subprocess.TimeoutExpired as exc:
        raise LinuxChiakiError(f"Timed out running {command[0]!r}.") from exc


class LinuxChiakiApp:
    """Own or attach to Chiaki-ng and expose its window as a snapshot source."""

    def __init__(self, root: Path | None = None, binary: Path | None = None):
        self.root = root or capture.state_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.latest = self.root / "latest.jpg"
        self.log = self.root / "chiaki-ng.log"
        self.pidfile = self.root / "chiaki-ng.pid"
        configured = os.environ.get("PS5MCP_CHIAKI")
        self.binary = Path(configured) if configured else binary or (
            Path(__file__).resolve().parents[2] / "build/linux-chiaki/gui/chiaki"
        )
        self.env = _display_environment()
        self.process: subprocess.Popen | None = None
        self._owns_process = False
        self._lock = threading.RLock()
        self._keys: set[str] = set()
        self._state = PadState()

    def _window(self) -> int:
        if not self.env.get("DISPLAY"):
            raise LinuxChiakiError("No X11/Xwayland display is available to this Linux session.")
        result = _run(["xdotool", "search", "--onlyvisible", "--name", "chiaki-ng"], env=self.env)
        ids = [line for line in result.stdout.decode(errors="replace").splitlines() if line.isdigit()]
        if result.returncode or not ids:
            raise LinuxChiakiError("Chiaki-ng has no visible window. Open the PS4 Remote Play session first.")
        # A stream pop-up may coexist with the host-list window. Use the largest visible window.
        windows: list[tuple[int, int]] = []
        for item in ids:
            geometry = _run(["xdotool", "getwindowgeometry", "--shell", item], env=self.env)
            values = {}
            for line in geometry.stdout.decode(errors="replace").splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
            area = int(values.get("WIDTH", "0")) * int(values.get("HEIGHT", "0"))
            windows.append((area, int(item)))
        return max(windows)[1]

    def pid(self) -> int | None:
        try:
            pid = int(self.pidfile.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            return self.process.pid if self.process and self.process.poll() is None else None

    def status(self) -> dict | None:
        try:
            self._window()
        except LinuxChiakiError:
            return None
        return {"pid": self.pid() or os.getpid(), "frames": int(self.latest.exists()), "source": "remoteplay",
                "frame_age": self.frame_age() if self.latest.exists() else -1.0, "visible": True}

    def frame_age(self) -> float | None:
        try:
            return time.time() - self.latest.stat().st_mtime
        except FileNotFoundError:
            return None

    def start(self, video: str | None = None, audio=None, timeout: float = 30,
              visible: bool = True, host: str | None = None) -> int:
        del video, audio, host
        with self._lock:
            try:
                self._window()
            except LinuxChiakiError:
                if not self.binary.is_file():
                    raise LinuxChiakiError(f"Chiaki-ng is missing at {self.binary}; build it or set PS5MCP_CHIAKI.")
                with self.log.open("ab") as log:
                    self.process = subprocess.Popen([str(self.binary)], env=self.env, stdin=subprocess.DEVNULL,
                                                    stdout=log, stderr=log, start_new_session=True)
                self._owns_process = True
                self.pidfile.write_text(f"{self.process.pid}\n")
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        raise LinuxChiakiError(f"Chiaki-ng exited ({self.process.returncode}); see {self.log}.")
                    try:
                        self._window()
                        break
                    except LinuxChiakiError:
                        time.sleep(0.2)
                else:
                    raise LinuxChiakiError(f"Chiaki-ng did not open a window; see {self.log}.")
            self.refresh()
            return self.pid() or os.getpid()

    def capture_frame(self, max_width: int | None = None) -> Path:
        with self._lock:
            window = self._window()
            xwd_path = self.root / "chiaki-frame.xwd"
            xwd = _run(["xwd", "-silent", "-id", str(window), "-out", str(xwd_path)], env=self.env)
            if xwd.returncode:
                raise LinuxChiakiError(xwd.stderr.decode(errors="replace").strip() or "Could not capture Chiaki-ng.")
            raw = xwd_path.read_bytes()
            xwd_path.unlink(missing_ok=True)
            # Pipe through Netpbm in-memory so no temporary frames land in /tmp.
            decoded = subprocess.run(["xwdtopnm"], input=raw, capture_output=True, env=self.env, timeout=10)
            if decoded.returncode:
                raise LinuxChiakiError(decoded.stderr.decode(errors="replace").strip() or "Could not decode XWD frame.")
            reduced = subprocess.run(["pnmdepth", "255"], input=decoded.stdout, capture_output=True,
                                     env=self.env, timeout=10)
            if reduced.returncode:
                raise LinuxChiakiError(reduced.stderr.decode(errors="replace").strip() or "Could not normalize frame.")
            ppm_data = reduced.stdout
            if max_width:
                geometry = _run(["xdotool", "getwindowgeometry", "--shell", str(window)], env=self.env)
                width = next((int(line[6:]) for line in geometry.stdout.decode().splitlines()
                              if line.startswith("WIDTH=")), 0)
                if width > max_width:
                    scaled = subprocess.run(["pnmscale", "-width", str(max_width)], input=ppm_data,
                                            capture_output=True, env=self.env, timeout=10)
                    if scaled.returncode:
                        raise LinuxChiakiError("Could not scale the Chiaki-ng frame.")
                    ppm_data = scaled.stdout
            jpeg = subprocess.run(["pnmtojpeg", "--quality=85"], input=ppm_data, capture_output=True,
                                  env=self.env, timeout=10)
            if jpeg.returncode:
                raise LinuxChiakiError(jpeg.stderr.decode(errors="replace").strip() or "Could not encode JPEG frame.")
            self.latest.write_bytes(jpeg.stdout)
            return self.latest

    def refresh(self, max_width: int | None = 1280) -> None:
        self.capture_frame(max_width=max_width)

    def stop(self, grace: float = 3) -> bool:
        del grace
        if not self._owns_process or not self.process or self.process.poll() is not None:
            return False
        self.process.send_signal(signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=2)
        if self.process.poll() is None:
            self.process.kill()
        self.pidfile.unlink(missing_ok=True)
        return True

    def show(self) -> None:
        window = self._window()
        _run(["xdotool", "windowraise", str(window), "windowfocus", "--sync", str(window)], env=self.env)


class LinuxChiakiClient:
    """Small controller facade using Chiaki-ng's built-in keyboard-to-pad mapping."""

    def __init__(self, app: LinuxChiakiApp):
        self.app = app
        self.closed = False

    def _keys_for(self, state: PadState) -> set[str]:
        keys = {key for bit, key in BUTTON_KEYS.items() if state.buttons & bit}
        for axis, (negative, positive) in AXIS_KEYS.items():
            value = getattr(state, axis)
            if value < CENTER - 16:
                keys.add(negative)
            elif value > CENTER + 16:
                keys.add(positive)
        if state.l2:
            keys.add(BUTTON_KEYS[0x000100])
        if state.r2:
            keys.add(BUTTON_KEYS[0x000200])
        return keys

    def set(self, state: PadState, wait: float = 0) -> None:
        del wait
        with self.app._lock:
            window = self.app._window()
            self.app.show()
            next_keys = self._keys_for(state)
            for key in sorted(self.app._keys - next_keys):
                _run(["xdotool", "keyup", "--clearmodifiers", key], env=self.app.env)
            for key in sorted(next_keys - self.app._keys):
                _run(["xdotool", "keydown", "--clearmodifiers", key], env=self.app.env)
            self.app._keys = next_keys
            self.app._state = state

    def press(self, state: PadState, hold_ms: int, wait: float = 0) -> None:
        self.set(state, wait)
        time.sleep(max(0, hold_ms) / 1000)
        self.release()

    def release(self) -> None:
        self.set(PadState())

    def release_all(self) -> None:
        self.release()

    def call(self, cmd: str, **fields) -> dict:
        if cmd == "status":
            return {"pad": {"connected": True, "host": os.environ.get("PS4_HOST") or os.environ.get("PS5_HOST", ""), "port": 0,
                             "error": None, "reconnects": 0, "human_has_control": False,
                             "agent_layers": 0},
                    "source": "remoteplay", "frame_age": self.app.frame_age() or 0,
                    "pid": self.app.pid() or os.getpid(), "visible": True, "clients": 1,
                    "version": "Linux Chiaki-ng", "build": "linux", "notice": "",
                    "padd": {"bundled": None},
                    "remote_play": {"state": "streaming", "video_ready": True,
                                    "keyboard": {"enabled": True, "open": False}},
                    "frame_meter": None, "controllers": None, "login_users": None}
        if cmd == "state":
            from .hub import state_to_json
            return {"state": state_to_json(self.app._state)}
        if cmd == "subscribe":
            return {"ok": True}
        if cmd in ("claim", "lease"):
            return {"granted": True, "position": 0, "owner": None, "queue": []}
        if cmd == "unclaim":
            return {"granted": False, "position": 0, "owner": None, "queue": []}
        if cmd == "ping":
            return {"pong": True}
        if cmd == "show":
            self.app.show()
            return {"ok": True}
        if cmd in ("release", "release_all"):
            self.release()
            return {"ok": True}
        if cmd == "set":
            from .hub import state_from_json
            self.set(state_from_json(fields["state"]), float(fields.get("wait", 0)))
            return {"ok": True}
        if cmd == "press":
            from .hub import state_from_json
            self.press(state_from_json(fields["state"]), int(fields["hold_ms"]), float(fields.get("wait", 0)))
            return {"ok": True}
        if cmd == "rp_button":
            if fields.get("button") != "ps":
                raise LinuxChiakiError("Chiaki-ng Linux backend only exposes the PS button through Remote Play.")
            self.press(PadState(buttons=0x010000), int(fields.get("hold_ms", 100)))
            return {"ok": True}
        raise LinuxChiakiError(f"MCP operation {cmd!r} still needs a PS4-native implementation.")

    def on(self, event: str, handler) -> None:
        del event, handler

    def subscribe(self, *events: str) -> dict:
        del events
        return {"ok": True}

    def lease(self) -> dict:
        return {"granted": True, "position": 0, "owner": None, "queue": []}

    def claim(self, reason: str = "", idle_s: float | None = None) -> dict:
        del reason, idle_s
        return self.lease()

    def unclaim(self) -> dict:
        return self.lease()

    def ping(self) -> bool:
        return True

    def command(self, op: str, arg: str = "", wait: float = 0) -> int:
        del op, arg, wait
        raise LinuxChiakiError("PS5 payload commands are not available for a PS4 Remote Play session.")

    def keyboard_text(self, text: str, submit: bool = False, timeout_s: float = 3) -> dict:
        del text, submit, timeout_s
        raise LinuxChiakiError("Remote Play keyboard text needs the Linux-native Chiaki control API.")

    def keyboard_close(self, accept: bool) -> None:
        del accept
        raise LinuxChiakiError("Remote Play keyboard controls are not implemented for the Linux backend.")

    def close(self) -> None:
        self.release()
        self.closed = True


def make_client(app: LinuxChiakiApp) -> LinuxChiakiClient:
    return LinuxChiakiClient(app)
