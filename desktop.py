"""Continuity Studio — Windows desktop launcher.

Boots the FastAPI server on a free localhost port in a background thread, then
opens a native desktop window pointed at it (pywebview). Falls back to the
system default browser if pywebview is unavailable.

Packaged-app aware:
  * All WRITABLE state (data/, vault.json, users.json, codes.json) is redirected
    to %LOCALAPPDATA%\\ContinuityStudio so it works after install into the
    read-only Program Files directory.
  * Bundled ffmpeg/ffprobe (next to the frozen exe under ``ffmpeg/``) is
    prepended to PATH so the app finds them with no system install.

This module sets the environment BEFORE importing the app so config.py and
store.py pick up the writable locations at import time.
"""
import os
import sys
import socket
import threading
import time


def _fix_stdio():
    """PyInstaller-built GUI apps (console=False) have sys.stdout/stderr set
    to None. Uvicorn's default formatter calls ``sys.stdout.isatty()`` during
    logging configuration and crashes with
    ``AttributeError: 'NoneType' object has no attribute 'isatty'``.

    Re-bind them to a real writable stream (a log file + a teed mirror) so
    uvicorn + everything that touches ``sys.stdout`` works as in a console
    build.  We also keep a copy in app.log so a frozen user can ask for
    support without losing startup output.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return  # already real streams (dev / console build)

    state = os.environ.get("CS_CONFIG_DIR") or os.environ.get(
        "LOCALAPPDATA", os.path.expanduser("~"))
    log_dir = os.path.join(state, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "app.log")

    class _Tee:
        """Write to two file streams. isatty() always returns False so
        uvicorn / other libs never try to call tty-only methods."""
        def __init__(self, *streams):
            self._streams = streams
        def write(self, s):
            for st in self._streams:
                try:
                    st.write(s)
                    st.flush()
                except Exception:
                    pass
        def flush(self):
            for st in self._streams:
                try:
                    st.flush()
                except Exception:
                    pass
        def isatty(self):
            return False
        def close(self):
            for st in self._streams:
                try:
                    st.close()
                except Exception:
                    pass

    devnull = open(os.devnull, "w")
    log_file = open(log_path, "a", encoding="utf-8", errors="replace")
    log_file.write(f"\n--- launch {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log_file.flush()
    sys.stdout = _Tee(devnull, log_file)
    sys.stderr = _Tee(devnull, log_file)


def _app_root():
    """Directory that holds bundled read-only resources (static/, ffmpeg/)."""
    if getattr(sys, "frozen", False):
        # PyInstaller: resources are unpacked next to the exe (one-folder) or
        # into _MEIPASS (one-file). _MEIPASS covers both for added datas.
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _writable_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "ContinuityStudio")
    os.makedirs(d, exist_ok=True)
    return d


def _free_port(preferred=8765):
    """Return a usable localhost port — try the preferred one first."""
    for p in (preferred, 8000, 8765, 8123, 0):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", p))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            continue
    return 8000


def _prepare_env():
    root = _app_root()
    state = _writable_dir()

    # Writable locations (read by config.py / app.py at import).
    os.environ.setdefault("DATA_DIR", os.path.join(state, "data"))
    os.environ.setdefault("CS_CONFIG_DIR", state)

    # Bundled ffmpeg/ffprobe -> PATH (no system install required).
    ff = os.path.join(root, "ffmpeg")
    if os.path.isdir(ff):
        os.environ["PATH"] = ff + os.pathsep + os.environ.get("PATH", "")

    # Make sure the working dir is the resource root so any remaining relative
    # lookups (static/index.html via __file__ already absolute) behave.
    try:
        os.chdir(root)
    except Exception:
        pass
    return root, state


def _run_server(port):
    try:
        import uvicorn
        # Import AFTER env is prepared so DATA_DIR / CS_CONFIG_DIR take effect.
        # ui_patch also installs the runtime auth/data hardening layer.
        import ui_patch as _app
        uvicorn.run(_app.app, host="127.0.0.1", port=port, log_level="warning")
    except Exception as exc:
        # Server crashed — write to a log file so the user (and support) can
        # see WHY.  Without this, a frozen GUI app (console=False) swallows
        # the traceback silently and _wait_ready() just times out.
        import traceback, pathlib
        state = os.environ.get("CS_CONFIG_DIR") or os.environ.get(
            "LOCALAPPDATA", os.path.expanduser("~"))
        log_dir = os.path.join(state, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "server-crash.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Continuity Studio server failed to start on port {port}\n")
            f.write(f"Python {sys.version}\n")
            f.write(f"EXE: {sys.executable}\n")
            f.write(f"Frozen: {getattr(sys, 'frozen', False)}\n\n")
            traceback.print_exc(file=f)
        # Also show a MessageBox so the user sees the error immediately.
        _show_error(
            "Continuity Studio — server crash",
            f"The internal server crashed on startup.\n\n"
            f"Error: {exc}\n\n"
            f"Full log: {log_path}",
        )


def _wait_ready(url, timeout=40.0):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def _show_error(title, message):
    """Last-resort user-visible error when the GUI window can't open."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)  # MB_ICONERROR
    except Exception:
        print(f"[{title}] {message}", file=sys.stderr)


def main():
    _fix_stdio()           # MUST run before any uvicorn import / app launch
    # Suppress CMD window flashes from ffmpeg / yt-dlp / whisper subprocesses.
    try:
        import nowindow
        nowindow.install()
    except Exception:
        pass
    _prepare_env()
    port = _free_port(8000)
    url = f"http://127.0.0.1:{port}/"

    t = threading.Thread(target=_run_server, args=(port,), daemon=True)
    t.start()

    if not _wait_ready(url, timeout=90):
        _show_error(
            "Continuity Studio",
            f"The internal server didn't start in time on {url}.\n"
            "Please relaunch, or check Settings → reset the vault.\n"
            "Logs: %LOCALAPPDATA%\\ContinuityStudio\\logs\\",
        )
        return

    # Preferred: native desktop window via pywebview. Try the modern
    # EdgeChromium backend first (uses the WebView2 runtime you already have
    # for Edge); fall back to older backends; finally to the system browser if
    # no GUI backend works on this machine.
    try:
        import webview
        # Try EdgeChromium explicitly (skips WinForms/IE-mode which is broken
        # on many current Edge configs).
        gui = None
        for backend in ("edgechromium", "mshtml", "winforms", None):
            try:
                kwargs = {"title": "Continuity Studio",
                          "url": url,
                          "width": 1480,
                          "height": 940,
                          "min_size": (1024, 680),
                          "confirm_close": False}
                if backend:
                    kwargs["gui"] = backend
                webview.create_window(**kwargs)
                webview.start()
                gui = backend or "default"
                break
            except Exception as be:
                last_err = be
                continue
        if gui is None:
            raise RuntimeError(f"no working pywebview backend: {last_err!r}")
        return
    except Exception as e:
        # Windowless / install error: fall back to the system browser so the
        # user can still use the app, AND pop a clear message so they know
        # WHY the native window didn't open (most often: WebView2 runtime
        # missing on a clean Windows install).
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
        _show_error(
            "Continuity Studio — opened in browser",
            "The native desktop window couldn't open (most often: the WebView2 "
            "runtime is missing).\n\n"
            "The app is running at:\n"
            f"  {url}\n\n"
            "It opened in your default browser so you can still use it.\n\n"
            "To get the native window, install the WebView2 runtime "
            "(Microsoft Edge installs it automatically, or grab the "
            "Evergreen Standalone Installer from Microsoft).\n\n"
            f"Details: {e!r}",
        )
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
