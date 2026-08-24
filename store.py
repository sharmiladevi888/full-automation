"""File-based persistence with multiple projects.

Layout:
    data/
      projects.json        <- index: {current, projects:[{id,name,created,updated}]}
      projects/<id>.json   <- one file per project (the whole project state)
      project.json         <- legacy single-project state (auto-migrated on init)
      images/ characters/ frames/ uploads/ audio/ videos/   <- shared media

Media files carry unique ids and are referenced by /data/... URLs, so they're
shared across projects safely; switching a project only swaps which state file
is active.
"""
import html
import json
import os
import threading
import time
import uuid

import config

# Serializes read-modify-write of the project index (projects.json) so
# concurrent background workers (e.g. the image queue) can't clobber each
# other's index updates. Reentrant: some mutators call _add_project nested.
_INDEX_LOCK = threading.RLock()

DATA_DIR = config.DATA_DIR
IMAGES_DIR = os.path.join(DATA_DIR, "images")
CHARS_DIR = os.path.join(DATA_DIR, "characters")
FRAMES_DIR = os.path.join(DATA_DIR, "frames")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
AUDIO_DIR = os.path.join(DATA_DIR, "audio")
VIDEOS_DIR = os.path.join(DATA_DIR, "videos")
PROJECTS_DIR = os.path.join(DATA_DIR, "projects")
INDEX_PATH = os.path.join(DATA_DIR, "projects.json")
STATE_PATH = os.path.join(DATA_DIR, "project.json")   # legacy

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
    "music": None,          # {id, url, name, duration, volume}
    "brand": None,          # {accent, handle, logo_url}
    "sfx": [],              # [{id, url, name, duration, at_seconds, volume}]
    "voice_map": {},         # {character_name: voice_id}
}


def new_id(prefix="id"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def now():
    return int(time.time())


def _default_state():
    return json.loads(json.dumps(_DEFAULT_STATE))


# --------------------------------------------------------------------------- #
#  Project index
# --------------------------------------------------------------------------- #
def _read_index():
    if not os.path.exists(INDEX_PATH):
        return {"current": None, "projects": []}
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"current": None, "projects": []}


def _write_index(idx):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = INDEX_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, indent=2)
        os.replace(tmp, INDEX_PATH)
    except OSError as e:
        # Surface disk-full / permission errors clearly instead of raw OSError
        _errno = getattr(e, "errno", 0)
        if _errno == 28:  # ENOSPC
            raise RuntimeError("Disk full — cannot save project index. Free up space and retry.") from e
        if _errno == 13:  # EACCES
            raise RuntimeError(f"Permission denied saving project index: {INDEX_PATH}") from e
        raise RuntimeError(f"Could not save project index: {e}") from e


def _project_path(pid):
    return os.path.join(PROJECTS_DIR, f"{pid}.json")


def _save_project(pid, state):
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    p = _project_path(pid)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)
    except OSError as e:
        _errno = getattr(e, "errno", 0)
        if _errno == 28:
            raise RuntimeError(f"Disk full — cannot save project {pid}. Free up space and retry.") from e
        if _errno == 13:
            raise RuntimeError(f"Permission denied saving project {pid}: {p}") from e
        raise RuntimeError(f"Could not save project {pid}: {e}") from e


def _add_project(name, state=None, make_current=True):
    pid = new_id("proj")
    _save_project(pid, state if state is not None else _default_state())
    with _INDEX_LOCK:
        idx = _read_index()
        idx.setdefault("projects", []).append({
            "id": pid, "name": (name or "Untitled project").strip()[:80] or "Untitled project",
            "created": now(), "updated": now(),
        })
        if make_current or not idx.get("current"):
            idx["current"] = pid
        _write_index(idx)
    return pid


def init():
    for d in (DATA_DIR, IMAGES_DIR, CHARS_DIR, FRAMES_DIR, UPLOADS_DIR,
              AUDIO_DIR, VIDEOS_DIR, PROJECTS_DIR):
        os.makedirs(d, exist_ok=True)
    idx = _read_index()
    if not idx.get("projects"):
        # Migrate a legacy single-project file, else start a blank project.
        legacy = None
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    legacy = json.load(f)
            except Exception:
                legacy = None
        _add_project("My first project", legacy, make_current=True)


def current_project_id():
    with _INDEX_LOCK:
        idx = _read_index()
        pid = idx.get("current")
        ids = [p["id"] for p in idx.get("projects", [])]
        if pid in ids:
            return pid
        if ids:
            idx["current"] = ids[0]
            _write_index(idx)
            return ids[0]
        # Create first project INSIDE the lock to prevent duplicate creation
        # when two concurrent requests both see an empty index.
        return _add_project("My first project")


# --------------------------------------------------------------------------- #
#  Current-project state I/O  (same API the rest of the app already uses)
# --------------------------------------------------------------------------- #
def load_state():
    pid = current_project_id()
    p = _project_path(pid)
    if not os.path.exists(p):
        _save_project(pid, _default_state())
    with open(p, "r", encoding="utf-8") as f:
        st = json.load(f)
    for k, v in _DEFAULT_STATE.items():
        st.setdefault(k, json.loads(json.dumps(v)))
    return st


def save_state(state):
    pid = current_project_id()
    _save_project(pid, state)
    with _INDEX_LOCK:
        idx = _read_index()
        for p in idx.get("projects", []):
            if p["id"] == pid:
                p["updated"] = now()
        _write_index(idx)
    return state


# Explicit-project variants — used by background workers (e.g. the image queue)
# so results always land in the project that was active when the batch was
# submitted, even if the user switches projects mid-run.
def load_state_for(pid):
    if not pid:
        return load_state()
    p = _project_path(pid)
    if not os.path.exists(p):
        return _default_state()
    with open(p, "r", encoding="utf-8") as f:
        st = json.load(f)
    for k, v in _DEFAULT_STATE.items():
        st.setdefault(k, json.loads(json.dumps(v)))
    return st


def save_state_for(pid, state):
    if not pid:
        return save_state(state)
    _save_project(pid, state)
    with _INDEX_LOCK:
        idx = _read_index()
        for p in idx.get("projects", []):
            if p["id"] == pid:
                p["updated"] = now()
        _write_index(idx)
    return state


# --------------------------------------------------------------------------- #
#  Project management
# --------------------------------------------------------------------------- #
def list_projects():
    idx = _read_index()
    projects = sorted(idx.get("projects", []),
                      key=lambda p: p.get("updated", 0), reverse=True)
    return {"current": idx.get("current"), "projects": projects}


def create_project(name="", master_prompt=""):
    st = _default_state()
    if master_prompt:
        st["master_prompt"] = master_prompt
    return _add_project(name, st, make_current=True)


def duplicate_project(pid):
    src = _project_path(pid)
    if not os.path.exists(src):
        raise ValueError("no such project")
    with open(src, "r", encoding="utf-8") as f:
        state = json.load(f)
    idx = _read_index()
    nm = next((p["name"] for p in idx.get("projects", []) if p["id"] == pid), "Project")
    return _add_project(f"Copy of {nm}"[:80], json.loads(json.dumps(state)),
                        make_current=True)


def switch_project(pid):
    with _INDEX_LOCK:
        idx = _read_index()
        if not any(p["id"] == pid for p in idx.get("projects", [])):
            raise ValueError("no such project")
        idx["current"] = pid
        _write_index(idx)
    return pid


def rename_project(pid, name):
    # Project names are rendered by the UI with innerHTML. Escape at the
    # persistence boundary as defense in depth; creation already sanitizes in
    # app.py, while this protects the rename endpoint as well.
    safe_name = html.escape((name or "").strip()[:80], quote=True)
    with _INDEX_LOCK:
        idx = _read_index()
        for p in idx.get("projects", []):
            if p["id"] == pid:
                p["name"] = safe_name or p["name"]
                p["updated"] = now()
        _write_index(idx)


def delete_project(pid):
    with _INDEX_LOCK:
        idx = _read_index()
        idx["projects"] = [p for p in idx.get("projects", []) if p["id"] != pid]
        try:
            os.remove(_project_path(pid))
        except OSError:
            pass
        if idx.get("current") == pid:
            idx["current"] = idx["projects"][0]["id"] if idx["projects"] else None
        _write_index(idx)
        empty = not idx["projects"]
    if empty:
        _add_project("My first project")   # always keep at least one


# --------------------------------------------------------------------------- #
#  Media helpers
# --------------------------------------------------------------------------- #
_FOLDER = {
    "images": IMAGES_DIR,
    "characters": CHARS_DIR,
    "frames": FRAMES_DIR,
    "audio": AUDIO_DIR,
    "videos": VIDEOS_DIR,
}


def write_image(kind, data, ext="png"):
    folder = _FOLDER[kind]
    fname = f"{new_id(kind.rstrip('s'))}.{ext}"
    path = os.path.join(folder, fname)
    try:
        with open(path, "wb") as f:
            f.write(data)
    except OSError as e:
        _errno = getattr(e, "errno", 0)
        if _errno == 28:
            raise RuntimeError(f"Disk full — cannot save {kind} image. Free up space.") from e
        raise RuntimeError(f"Could not save {kind} image: {e}") from e
    rel = os.path.relpath(path, DATA_DIR).replace(os.sep, "/")
    return f"/data/{rel}"


def write_binary(kind, data, ext, name_hint=None):
    folder = _FOLDER[kind]
    base = new_id(kind.rstrip("s"))
    if name_hint:
        safe = "".join(c for c in name_hint if c.isalnum() or c in "._-")[:60]
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
        _errno = getattr(e, "errno", 0)
        if _errno == 28:
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
    # Containment check: reject any path that escapes DATA_DIR via ../ etc.
    if path != root and not path.startswith(root + os.sep):
        raise ValueError(f"path escapes data directory: {url!r}")
    return path


def read_image(url):
    with open(url_to_path(url), "rb") as f:
        return f.read()


# --------------------------------------------------------------------------- #
#  Usage logging
# --------------------------------------------------------------------------- #
USAGE_PATH = os.path.join(DATA_DIR, "usage.json")


def _read_usage():
    if not os.path.exists(USAGE_PATH):
        return []
    try:
        with open(USAGE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write_usage(entries):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = USAGE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f)
        os.replace(tmp, USAGE_PATH)
    except OSError:
        # Usage logging is non-critical — don't crash the pipeline on disk-full
        pass


def log_usage(kind, count=1, est_cost=0.0, project_id=None):
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
    entries = _read_usage()
    idx = _read_index()
    proj_names = {p["id"]: p["name"] for p in idx.get("projects", [])}

    totals = {}
    by_day = {}
    by_project = {}
    for e in entries:
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

        p = by_project.setdefault(pid, {"name": proj_names.get(pid, pid), "count": 0, "est_cost": 0})
        p["count"] += c
        p["est_cost"] = round(p["est_cost"] + cost, 4)

    return {
        "totals": totals,
        "by_day": dict(sorted(by_day.items())),
        "by_project": by_project,
        "entries_count": len(entries),
    }
