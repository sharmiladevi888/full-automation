"""File-based persistence with multiple projects and optional account scoping.

Unauthenticated/local runs keep the original data/ layout. When the hardened
launcher supplies a signed-in account scope, each account gets an isolated
subtree under data/users/<sha256-prefix>/ so project state, media and usage
records cannot be shared accidentally between accounts.
"""
import hashlib
import html
import json
import os
import re
import tempfile
import threading
import time
import uuid
from contextvars import ContextVar

import config

DATA_DIR = config.DATA_DIR

# The request middleware serializes scoped API/data requests so these legacy
# module-level path attributes remain safe for older app.py call sites that
# access store.UPLOADS_DIR directly. All persistence helpers also derive paths
# from the ContextVar, so worker threads can set their own scope explicitly.
_SCOPE = ContextVar("continuity_store_scope", default="")
_SCOPE_DIR_NAME = "users"
_SCOPE_ID_LEN = 24
_MEDIA_KINDS = ("images", "characters", "frames", "uploads", "audio", "videos")

# One re-entrant lock protects all JSON state/index read-modify-write cycles in
# this process. Atomic replacement prevents torn files; the request wrapper
# keeps a complete endpoint mutation together so load -> mutate -> save does
# not lose updates to another request.
_STORE_LOCK = threading.RLock()
_INDEX_LOCK = _STORE_LOCK  # backwards-compatible name used by older code


def _scope_value():
    return _SCOPE.get() or ""


def scope_id(email=None):
    """Return the opaque on-disk scope id for an email/current scope."""
    value = _scope_value() if email is None else str(email or "").strip().lower()
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_SCOPE_ID_LEN]


def current_scope():
    """Return the current normalized account scope, or empty for local mode."""
    return _scope_value()


def set_user_scope(email):
    """Set the account scope for the current execution context.

    Returns the ContextVar token; callers must pass it to reset_user_scope() in
    a finally block. The compatibility path constants are refreshed for legacy
    app.py references while the request lock is held by the runtime wrapper.
    """
    token = _SCOPE.set(str(email or "").strip().lower())
    _refresh_compat_paths()
    return token


def reset_user_scope(token):
    """Restore the previous account scope and compatibility paths."""
    _SCOPE.reset(token)
    _refresh_compat_paths()


def _root_dir():
    sid = scope_id()
    if sid:
        return os.path.join(DATA_DIR, _SCOPE_DIR_NAME, sid)
    return DATA_DIR


def scope_url_prefix(email=None):
    """Return the only /data URL prefix allowed for the current account."""
    sid = scope_id(email)
    return f"/data/{_SCOPE_DIR_NAME}/{sid}/" if sid else "/data/"


def _path_map(root=None):
    root = root or _root_dir()
    out = {f"{kind.upper()}_DIR": os.path.join(root, kind)
           for kind in _MEDIA_KINDS}
    out.update({
        "PROJECTS_DIR": os.path.join(root, "projects"),
        "INDEX_PATH": os.path.join(root, "projects.json"),
        "STATE_PATH": os.path.join(root, "project.json"),
        "USAGE_PATH": os.path.join(root, "usage.json"),
    })
    return out


def _refresh_compat_paths():
    """Refresh module attributes retained for the existing app.py API."""
    paths = _path_map()
    globals().update(paths)
    if "_FOLDER" in globals():
        globals()["_FOLDER"] = {
            kind: paths[f"{kind.upper()}_DIR"]
            for kind in ("images", "characters", "frames", "audio", "videos")
        }


# Initialize compatibility paths for local/legacy mode.
_refresh_compat_paths()

_DEFAULT_STATE = {
    "master_prompt": "",
    "style_frames": [],
    "characters": [],
    "sequence": [],
    "script": None,
    "suggested_prompts": [],
    "audio": None,
    "voiceover": None,
    "edits": [],
    "yt_inspiration": None,
    "yt_analysis": None,
    "thumbnails": [],
    "music": None,
    "brand": None,
    "sfx": [],
    "voice_map": {},
}


def new_id(prefix="id"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def now():
    return int(time.time())


def _default_state():
    return json.loads(json.dumps(_DEFAULT_STATE))


def _atomic_write_json(path, payload):
    """Write JSON via fsync + same-directory replace."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
#  Project index
# --------------------------------------------------------------------------- #
def _read_index():
    path = os.path.join(_root_dir(), "projects.json")
    if not os.path.exists(path):
        return {"current": None, "projects": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"current": None, "projects": []}
    except Exception:
        return {"current": None, "projects": []}


def _write_index(idx):
    path = os.path.join(_root_dir(), "projects.json")
    try:
        _atomic_write_json(path, idx)
    except OSError as e:
        errno = getattr(e, "errno", 0)
        if errno == 28:
            raise RuntimeError("Disk full — cannot save project index. Free up space and retry.") from e
        if errno == 13:
            raise RuntimeError(f"Permission denied saving project index: {path}") from e
        raise RuntimeError(f"Could not save project index: {e}") from e


def _project_path(pid):
    # IDs are generated locally and must never be allowed to become path
    # components supplied by a request. This also closes the historical
    # ../../ traversal in duplicate/load/delete project operations.
    if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", pid):
        raise ValueError("invalid project id")
    return os.path.join(_root_dir(), "projects", f"{pid}.json")


def _save_project(pid, state):
    p = _project_path(pid)
    try:
        _atomic_write_json(p, state)
    except OSError as e:
        errno = getattr(e, "errno", 0)
        if errno == 28:
            raise RuntimeError(f"Disk full — cannot save project {pid}. Free up space and retry.") from e
        if errno == 13:
            raise RuntimeError(f"Permission denied saving project {pid}: {p}") from e
        raise RuntimeError(f"Could not save project {pid}: {e}") from e


def _add_project(name, state=None, make_current=True):
    pid = new_id("proj")
    with _STORE_LOCK:
        _save_project(pid, state if state is not None else _default_state())
        idx = _read_index()
        idx.setdefault("projects", []).append({
            "id": pid,
            "name": (name or "Untitled project").strip()[:80] or "Untitled project",
            "created": now(),
            "updated": now(),
        })
        if make_current or not idx.get("current"):
            idx["current"] = pid
        _write_index(idx)
    return pid


def init():
    # Initialize the local/legacy tree at import time. Scoped trees are created
    # lazily on the first request for each account.
    _refresh_compat_paths()
    for d in (DATA_DIR, IMAGES_DIR, CHARS_DIR, FRAMES_DIR, UPLOADS_DIR,
              AUDIO_DIR, VIDEOS_DIR, PROJECTS_DIR):
        os.makedirs(d, exist_ok=True)
    with _STORE_LOCK:
        idx = _read_index()
        if not idx.get("projects"):
            legacy = None
            legacy_path = os.path.join(DATA_DIR, "project.json")
            if not scope_id() and os.path.exists(legacy_path):
                try:
                    with open(legacy_path, "r", encoding="utf-8") as f:
                        legacy = json.load(f)
                except Exception:
                    legacy = None
            _add_project("My first project", legacy, make_current=True)


def current_project_id():
    with _STORE_LOCK:
        idx = _read_index()
        pid = idx.get("current")
        ids = [p.get("id") for p in idx.get("projects", []) if isinstance(p, dict)]
        if pid in ids:
            return pid
        if ids:
            idx["current"] = ids[0]
            _write_index(idx)
            return ids[0]
        return _add_project("My first project")


# --------------------------------------------------------------------------- #
#  Current-project state I/O
# --------------------------------------------------------------------------- #
def _normalize_state(st):
    if not isinstance(st, dict):
        st = _default_state()
    for k, v in _DEFAULT_STATE.items():
        st.setdefault(k, json.loads(json.dumps(v)))
    return st


def load_state():
    with _STORE_LOCK:
        pid = current_project_id()
        p = _project_path(pid)
        if not os.path.exists(p):
            _save_project(pid, _default_state())
        try:
            with open(p, "r", encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError, TypeError):
            st = _default_state()
        return _normalize_state(st)


def save_state(state):
    with _STORE_LOCK:
        pid = current_project_id()
        _save_project(pid, state)
        idx = _read_index()
        for p in idx.get("projects", []):
            if isinstance(p, dict) and p.get("id") == pid:
                p["updated"] = now()
        _write_index(idx)
    return state


def load_state_for(pid):
    if not pid:
        return load_state()
    with _STORE_LOCK:
        p = _project_path(pid)
        if not os.path.exists(p):
            return _default_state()
        try:
            with open(p, "r", encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError, TypeError):
            return _default_state()
        return _normalize_state(st)


def save_state_for(pid, state):
    if not pid:
        return save_state(state)
    with _STORE_LOCK:
        _save_project(pid, state)
        idx = _read_index()
        for p in idx.get("projects", []):
            if isinstance(p, dict) and p.get("id") == pid:
                p["updated"] = now()
        _write_index(idx)
    return state


# --------------------------------------------------------------------------- #
#  Project management
# --------------------------------------------------------------------------- #
def list_projects():
    with _STORE_LOCK:
        idx = _read_index()
        projects = sorted(
            [p for p in idx.get("projects", []) if isinstance(p, dict)],
            key=lambda p: p.get("updated", 0), reverse=True)
        return {"current": idx.get("current"), "projects": projects}


def create_project(name="", master_prompt=""):
    st = _default_state()
    if master_prompt:
        st["master_prompt"] = master_prompt
    return _add_project(name, st, make_current=True)


def duplicate_project(pid):
    with _STORE_LOCK:
        src = _project_path(pid)
        if not os.path.exists(src):
            raise ValueError("no such project")
        with open(src, "r", encoding="utf-8") as f:
            state = json.load(f)
        idx = _read_index()
        nm = next((p.get("name") for p in idx.get("projects", [])
                   if isinstance(p, dict) and p.get("id") == pid), "Project")
        return _add_project(f"Copy of {nm}"[:80], json.loads(json.dumps(state)),
                            make_current=True)


def switch_project(pid):
    with _STORE_LOCK:
        idx = _read_index()
        if not any(isinstance(p, dict) and p.get("id") == pid
                   for p in idx.get("projects", [])):
            raise ValueError("no such project")
        idx["current"] = pid
        _write_index(idx)
    return pid


def rename_project(pid, name):
    safe_name = html.escape((name or "").strip()[:80], quote=True)
    with _STORE_LOCK:
        idx = _read_index()
        for p in idx.get("projects", []):
            if isinstance(p, dict) and p.get("id") == pid:
                p["name"] = safe_name or p.get("name") or "Untitled project"
                p["updated"] = now()
        _write_index(idx)


def delete_project(pid):
    with _STORE_LOCK:
        idx = _read_index()
        idx["projects"] = [p for p in idx.get("projects", [])
                           if isinstance(p, dict) and p.get("id") != pid]
        try:
            os.remove(_project_path(pid))
        except FileNotFoundError:
            pass
        if idx.get("current") == pid:
            idx["current"] = (idx["projects"][0].get("id")
                               if idx["projects"] else None)
        _write_index(idx)
        empty = not idx["projects"]
    if empty:
        _add_project("My first project")


# --------------------------------------------------------------------------- #
#  Media helpers
# --------------------------------------------------------------------------- #
_FOLDER = {
    "images": IMAGES_DIR,
    "characters": CHARACTERS_DIR,
    "frames": FRAMES_DIR,
    "audio": AUDIO_DIR,
    "videos": VIDEOS_DIR,
}


def _media_dir(kind):
    if kind not in _MEDIA_KINDS:
        raise ValueError(f"unknown media kind: {kind}")
    path = os.path.join(_root_dir(), kind)
    os.makedirs(path, exist_ok=True)
    return path


def write_image(kind, data, ext="png"):
    folder = _media_dir(kind)
    ext = re.sub(r"[^A-Za-z0-9]+", "", str(ext or "png")) or "png"
    fname = f"{new_id(kind.rstrip('s'))}.{ext}"
    path = os.path.join(folder, fname)
    try:
        with open(path, "wb") as f:
            f.write(data)
    except OSError as e:
        errno = getattr(e, "errno", 0)
        if errno == 28:
            raise RuntimeError(f"Disk full — cannot save {kind} image. Free up space.") from e
        raise RuntimeError(f"Could not save {kind} image: {e}") from e
    rel = os.path.relpath(path, DATA_DIR).replace(os.sep, "/")
    return f"/data/{rel}"


def write_binary(kind, data, ext, name_hint=None):
    folder = _media_dir(kind)
    ext = re.sub(r"[^A-Za-z0-9]+", "", str(ext or "bin")) or "bin"
    base = new_id(kind.rstrip("s"))
    if name_hint:
        safe = "".join(c for c in str(name_hint)
                       if c.isalnum() or c in "._-")[:60]
        fname = f"{base}_{safe}" if safe else f"{base}.{ext}"
        if not fname.endswith("." + ext):
            fname += "." + ext
    else:
        fname = f"{base}.{ext}"
    path = os.path.join(folder, fname)
    try:
        with open(path, "wb") as f:
            f.write(data)
    except OSError as e:
        errno = getattr(e, "errno", 0)
        if errno == 28:
            raise RuntimeError(f"Disk full — cannot save {kind} file. Free up space.") from e
        raise RuntimeError(f"Could not save {kind} file: {e}") from e
    rel = os.path.relpath(path, DATA_DIR).replace(os.sep, "/")
    return f"/data/{rel}", path


def url_to_path(url):
    if not url or not url.startswith("/data/"):
        raise ValueError(f"not a managed media url: {url!r}")
    rel = url[len("/data/"):]
    path = os.path.realpath(os.path.join(DATA_DIR, rel))
    root = os.path.realpath(DATA_DIR)
    if path != root and not path.startswith(root + os.sep):
        raise ValueError(f"path escapes data directory: {url!r}")
    sid = scope_id()
    if sid:
        tenant_root = os.path.realpath(_root_dir())
        if path != tenant_root and not path.startswith(tenant_root + os.sep):
            raise ValueError("media belongs to a different account")
    return path


def read_image(url):
    with open(url_to_path(url), "rb") as f:
        return f.read()


# --------------------------------------------------------------------------- #
#  Usage logging
# --------------------------------------------------------------------------- #
def _usage_path():
    return os.path.join(_root_dir(), "usage.json")


def _read_usage():
    path = _usage_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_usage(entries):
    try:
        _atomic_write_json(_usage_path(), entries)
    except OSError:
        # Usage logging is non-critical — don't crash the pipeline on disk-full.
        pass


def log_usage(kind, count=1, est_cost=0.0, project_id=None):
    with _STORE_LOCK:
        entries = _read_usage()
        entries.append({
            "ts": now(),
            "kind": kind,
            "count": count,
            "est_cost": round(est_cost, 4),
            "project_id": project_id or current_project_id(),
        })
        _write_usage(entries)


def get_usage():
    with _STORE_LOCK:
        entries = _read_usage()
        idx = _read_index()
        proj_names = {p.get("id"): p.get("name")
                      for p in idx.get("projects", []) if isinstance(p, dict)}

        totals, by_day, by_project = {}, {}, {}
        for e in entries:
            if not isinstance(e, dict):
                continue
            k = e.get("kind", "unknown")
            c = e.get("count", 1)
            cost = e.get("est_cost", 0)
            pid = e.get("project_id", "")
            day = time.strftime("%Y-%m-%d", time.localtime(e.get("ts", 0)))

            t = totals.setdefault(k, {"count": 0, "est_cost": 0})
            t["count"] += c
            t["est_cost"] = round(t["est_cost"] + cost, 4)

            d = by_day.setdefault(day, {})
            dk = d.setdefault(k, {"count": 0, "est_cost": 0})
            dk["count"] += c
            dk["est_cost"] = round(dk["est_cost"] + cost, 4)

            p = by_project.setdefault(pid, {
                "name": proj_names.get(pid, pid), "count": 0, "est_cost": 0})
            p["count"] += c
            p["est_cost"] = round(p["est_cost"] + cost, 4)

        return {
            "totals": totals,
            "by_day": dict(sorted(by_day.items())),
            "by_project": by_project,
            "entries_count": len(entries),
        }
