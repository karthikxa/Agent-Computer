"""Flask desktop API backed by xdotool, scrot, xclip, and Chromium."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_file

app = Flask(__name__)
os.environ.setdefault("DISPLAY", ":1")

UPLOAD_DIR = Path("/home/user/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _run(cmd: list[str], *, shell: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, shell=shell, capture_output=True, text=True, check=False)


def _json_error(exc: Exception, code: int = 500):
    return jsonify({"error": str(exc)}), code


@app.get("/health")
def health():
    """Return container health."""

    return jsonify({"status": "ok"})


@app.get("/screenshot")
def screenshot():
    """Take a PNG screenshot of the full desktop."""

    try:
        path = Path(tempfile.gettempdir()) / "ss.png"
        result = _run(["scrot", "-o", str(path)])
        if result.returncode != 0:
            raise RuntimeError(result.stderr or "scrot failed")
        return send_file(path, mimetype="image/png")
    except Exception as exc:
        return _json_error(exc)


@app.post("/click")
def click():
    """Left-click at coordinates."""

    try:
        body = request.get_json(force=True)
        x, y = int(body["x"]), int(body["y"])
        _run(["xdotool", "mousemove", str(x), str(y)])
        _run(["xdotool", "click", "1"])
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _json_error(exc)


@app.post("/double_click")
def double_click():
    """Double click at coordinates."""

    try:
        body = request.get_json(force=True)
        x, y = int(body["x"]), int(body["y"])
        _run(["xdotool", "mousemove", str(x), str(y)])
        _run(["xdotool", "click", "--repeat", "2", "1"])
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _json_error(exc)


@app.post("/type")
def type_text():
    """Type text into the active window."""

    try:
        body = request.get_json(force=True)
        _run(["xdotool", "type", "--clearmodifiers", body["text"]])
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _json_error(exc)


@app.post("/key")
def key():
    """Press keyboard shortcuts."""

    try:
        body = request.get_json(force=True)
        _run(["xdotool", "key", body["keys"]])
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _json_error(exc)


@app.post("/scroll")
def scroll():
    """Scroll the pointer wheel."""

    try:
        body = request.get_json(force=True)
        amount = int(body.get("amount", 3))
        direction = str(body.get("direction", "down")).lower()
        button = "5" if direction in {"down", "right"} else "4"
        for _ in range(amount):
            _run(["xdotool", "click", button])
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _json_error(exc)


@app.post("/drag")
def drag():
    """Drag from one point to another."""

    try:
        body = request.get_json(force=True)
        x1, y1, x2, y2 = map(int, (body["x1"], body["y1"], body["x2"], body["y2"]))
        _run(["xdotool", "mousemove", str(x1), str(y1)])
        _run(["xdotool", "mousedown", "1"])
        _run(["xdotool", "mousemove", str(x2), str(y2)])
        _run(["xdotool", "mouseup", "1"])
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _json_error(exc)


@app.post("/command")
def command():
    """Run a shell command."""

    try:
        body = request.get_json(force=True)
        result = subprocess.run(body["cmd"], shell=True, capture_output=True, text=True)
        return jsonify({"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode})
    except Exception as exc:
        return _json_error(exc)


@app.get("/screen_size")
def screen_size():
    """Return the current display size."""

    try:
        result = _run(["bash", "-lc", "xdpyinfo | grep dimensions"])
        text = result.stdout or result.stderr
        dims = text.split("dimensions:")[-1].strip().split()[0]
        width, height = dims.split("x")
        return jsonify({"width": int(width), "height": int(height)})
    except Exception as exc:
        return _json_error(exc)


@app.post("/browser/open")
def browser_open():
    """Launch Chromium in headed mode."""

    try:
        body = request.get_json(force=True, silent=True) or {}
        url = body.get("url", "about:blank")
        subprocess.Popen(["chromium-browser", "--new-window", url], env={**os.environ, "DISPLAY": ":1"})
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _json_error(exc)


@app.post("/browser/navigate")
def browser_navigate():
    """Navigate the active Chromium window."""

    try:
        body = request.get_json(force=True)
        url = body["url"]
        _run(["xdotool", "key", "ctrl+l"])
        _run(["xdotool", "type", "--clearmodifiers", url])
        _run(["xdotool", "key", "Return"])
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _json_error(exc)


@app.post("/browser/screenshot")
def browser_screenshot():
    """Capture a screenshot of the active browser window."""

    try:
        path = Path(tempfile.gettempdir()) / "browser.png"
        _run(["scrot", "-u", str(path)])
        return send_file(path, mimetype="image/png")
    except Exception as exc:
        return _json_error(exc)


@app.post("/clipboard/copy")
def clipboard_copy():
    """Copy the active selection."""

    try:
        _run(["xdotool", "key", "ctrl+c"])
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _json_error(exc)


@app.post("/clipboard/paste")
def clipboard_paste():
    """Paste clipboard contents."""

    try:
        _run(["xdotool", "key", "ctrl+v"])
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _json_error(exc)


@app.get("/clipboard/get")
def clipboard_get():
    """Read clipboard contents."""

    try:
        result = _run(["xclip", "-selection", "clipboard", "-o"])
        return jsonify({"text": result.stdout})
    except Exception as exc:
        return _json_error(exc)


@app.post("/file/upload")
def file_upload():
    """Store uploaded bytes on disk."""

    try:
        file = request.files["file"]
        filename = file.filename or "upload.bin"
        path = UPLOAD_DIR / filename
        file.save(path)
        return jsonify({"path": str(path)})
    except Exception as exc:
        return _json_error(exc)


@app.get("/file/download/<filename>")
def file_download(filename: str):
    """Download a file from the upload directory."""

    try:
        path = UPLOAD_DIR / filename
        return send_file(path, as_attachment=True)
    except Exception as exc:
        return _json_error(exc)


@app.post("/app/launch")
def app_launch():
    """Launch an application by name."""

    try:
        body = request.get_json(force=True)
        subprocess.Popen([body["name"]], env={**os.environ, "DISPLAY": ":1"})
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _json_error(exc)


@app.post("/app/close")
def app_close():
    """Close an application by window name."""

    try:
        body = request.get_json(force=True)
        _run(["xdotool", "search", "--name", body["name"], "windowkill"])
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _json_error(exc)


@app.get("/windows")
def windows():
    """List open windows."""

    try:
        result = _run(["wmctrl", "-l"])
        return jsonify({"windows": result.stdout.splitlines()})
    except Exception as exc:
        return _json_error(exc)


@app.post("/window/focus")
def window_focus():
    """Focus a window by title."""

    try:
        body = request.get_json(force=True)
        _run(["xdotool", "search", "--name", body["title"], "windowactivate"])
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _json_error(exc)


@app.post("/notify")
def notify():
    """Send a desktop notification."""

    try:
        body = request.get_json(force=True)
        _run(["bash", "-lc", f"notify-send {body['message']!r}"])
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _json_error(exc)


@app.get("/processes")
def processes():
    """List running processes."""

    try:
        result = _run(["ps", "-eo", "pid,ppid,cmd"])
        return jsonify({"processes": result.stdout.splitlines()})
    except Exception as exc:
        return _json_error(exc)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
