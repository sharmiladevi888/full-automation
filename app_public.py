"""Continuity Studio — Public edition (no login gate)."""
import os
from typing import List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import pipeline
import store
import editor
from derouter import ImageClient
from claude_client import ClaudeClient


class AnalyseIn(BaseModel):
    image_url: str
    question: str = ""

store.init()
app = FastAPI(title="Continuity Studio Public")


def public_settings():
    return {
        "api_key": os.environ.get("PUBLIC_DEROUTER_API_KEY", config.API_KEY or os.environ.get("DEROUTER_API_KEY", "")),
        "base_url": os.environ.get("PUBLIC_DEROUTER_BASE_URL", config.BASE_URL),
        "model": os.environ.get("PUBLIC_IMAGE_MODEL", config.MODEL),
        "multi_image_edit": config.MULTI_IMAGE_EDIT,
        "claude_api_key": os.environ.get("PUBLIC_CLAUDE_API_KEY", config.CLAUDE_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")),
        "claude_base_url": os.environ.get("PUBLIC_CLAUDE_BASE_URL", config.CLAUDE_BASE_URL),
        "claude_model": os.environ.get("PUBLIC_CLAUDE_MODEL", config.CLAUDE_MODEL),
        "elevenlabs_api_key": os.environ.get("PUBLIC_ELEVENLABS_API_KEY", getattr(config, "ELEVENLABS_API_KEY", "")),
        "elevenlabs_voice_id": os.environ.get("PUBLIC_ELEVENLABS_VOICE_ID", getattr(config, "ELEVENLABS_VOICE_ID", "")),
        "elevenlabs_model": os.environ.get("PUBLIC_ELEVENLABS_MODEL", getattr(config, "ELEVENLABS_MODEL", "")),
    }

public_settings_cached = public_settings()

app.mount("/data", StaticFiles(directory=config.DATA_DIR), name="data")


def get_image_client():
    s = public_settings_cached
    return ImageClient(api_key=s["api_key"], base_url=s["base_url"], model=s["model"])


def get_claude_client() -> ClaudeClient:
    s = public_settings_cached
    return ClaudeClient(api_key=s["claude_api_key"], base_url=s["claude_base_url"], model=s["claude_model"])


def get_voice_client(voice_id: str = None):
    import voice
    s = public_settings_cached
    return voice.VoiceClient(
        api_key=s["elevenlabs_api_key"],
        model=s["elevenlabs_model"],
        voice_id=voice_id or s["elevenlabs_voice_id"],
    )


@app.post("/api/analyse-scene")
def api_analyse_scene(a: AnalyseIn):
    try:
        img = store.read_image(a.image_url)
    except Exception as e:
        raise HTTPException(400, f"unreadable image: {e}")
    try:
        text = get_claude_client().analyse_scene(pipeline.downsize_for_vision(img), a.question)
    except Exception as e:
        raise HTTPException(500, f"analysis failed: {e}")
    return {"analysis": text}


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


@app.get("/api/state")
def api_state():
    defaults = {
        "model": public_settings_cached["model"],
        "base_url": public_settings_cached["base_url"],
        "has_api_key": bool(public_settings_cached["api_key"]),
        "multi_image_edit": public_settings_cached["multi_image_edit"],
        "claude_model": public_settings_cached["claude_model"],
        "claude_base_url": public_settings_cached["claude_base_url"],
        "has_claude_key": bool(public_settings_cached["claude_api_key"]),
        "claude_models": config.CLAUDE_MODELS,
        "default_size": config.DEFAULT_SIZE,
        "default_quality": config.DEFAULT_QUALITY,
        "sizes": config.SUPPORTED_SIZES,
        "qualities": config.SUPPORTED_QUALITIES,
    }
    return {"state": store.load_state(), "config": defaults}


@app.get("/api/health")
def api_health():
    s = public_settings_cached
    image_status = get_image_client().ping()
    image_status["multi_image_edit"] = s["multi_image_edit"]
    claude_status = get_claude_client().ping()
    try:
        voice_status = get_voice_client().ping()
    except Exception as e:
        voice_status = {"ok": False, "error": str(e)}
    return {"image": image_status, "claude": claude_status, "voice": voice_status}


class MasterIn(BaseModel):
    master_prompt: str = ""

@app.post("/api/master")
def api_master(m: MasterIn):
    st = store.load_state()
    st["master_prompt"] = m.master_prompt
    store.save_state(st)
    return {"ok": True}


@app.post("/api/video")
async def api_video(file: UploadFile = File(...), fps: float = Form(1.0), max_frames: int = Form(40)):
    import video as videomod
    dest = os.path.join(store.UPLOADS_DIR, store.new_id("upload") + "_" + (file.filename or "video.mp4"))
    with open(dest, "wb") as f:
        f.write(await file.read())
    try:
        urls = videomod.extract_frames(dest, fps=fps, max_frames=max_frames)
    except Exception as e:
        raise HTTPException(500, f"frame extraction failed: {e}")
    return {"frames": urls, "video_path": dest}


class StyleFramesIn(BaseModel):
    urls: List[str] = []

@app.post("/api/style-frames")
def api_style_frames(s: StyleFramesIn):
    st = store.load_state()
    st["style_frames"] = [{"id": store.new_id("frame"), "url": u} for u in s.urls]
    store.save_state(st)
    return {"ok": True, "count": len(st["style_frames"])}


@app.post("/api/scene-detect")
async def api_scene_detect(file: UploadFile = File(...), threshold: float = Form(0.4)):
    dest = os.path.join(store.UPLOADS_DIR, store.new_id("scene") + "_" + (file.filename or "video.mp4"))
    with open(dest, "wb") as f:
        f.write(await file.read())
    try:
        times = editor.detect_scenes(dest, threshold=threshold)
        dur = editor.probe_duration(dest)
    except Exception as e:
        raise HTTPException(500, f"scene detection failed: {e}")
    return {"scene_changes": times, "duration": dur, "video_path": dest}


class CharacterIn(BaseModel):
    name: str
    description: str = ""
    size: Optional[str] = None
    quality: Optional[str] = None

@app.post("/api/characters")
def api_create_character(c: CharacterIn):
    if not c.name.strip():
        raise HTTPException(400, "name is required")
    st = store.load_state()
    client = get_image_client()
    prompt = pipeline.build_sheet_prompt(st["master_prompt"], c.name, c.description)
    try:
        img = client.generate(prompt, size=c.size or config.DEFAULT_SIZE, quality=c.quality or config.DEFAULT_QUALITY)
    except Exception as e:
        raise HTTPException(500, f"sheet generation failed: {e}")
    rec = {
        "id": store.new_id("char"),
        "name": c.name.strip(),
        "description": c.description.strip(),
        "sheet_url": store.write_image("characters", img),
        "prompt": prompt,
        "source": "generated",
        "created": store.now(),
    }
    st["characters"].append(rec)
    store.save_state(st)
    return rec


class CharacterBatchIn(BaseModel):
    text: str
    size: Optional[str] = None
    quality: Optional[str] = None

@app.post("/api/characters/batch")
def api_create_characters_batch(b: CharacterBatchIn):
    # reusing the same logic as the private app but without per-user auth context
    entries = pipeline.parse_character_batch(b.text)
    if not entries:
        raise HTTPException(400, "no character entries found (separate with blank lines)")
    st = store.load_state()
    client = get_image_client()
    out = []
    for entry in entries:
        prompt = pipeline.build_sheet_prompt(st["master_prompt"], entry["name"], entry["description"])
        try:
            img = client.generate(prompt, size=b.size or config.DEFAULT_SIZE, quality=b.quality or config.DEFAULT_QUALITY)
        except Exception as e:
            raise HTTPException(500, f"sheet generation failed for {entry['name']}: {e}")
        rec = {
            "id": store.new_id("char"),
            "name": entry["name"].strip(),
            "description": entry["description"].strip(),
            "sheet_url": store.write_image("characters", img),
            "prompt": prompt,
            "source": "generated",
            "created": store.now(),
        }
        st["characters"].append(rec)
        out.append(rec)
    store.save_state(st)
    return out


class CharacterUploadIn(BaseModel):
    name: str
    description: str = ""
    file_name: Optional[str] = None

@app.post("/api/characters/upload")
async def api_upload_character(c: CharacterUploadIn, file: UploadFile = File(...)):
    contents = await file.read()
    ext = os.path.splitext(file.filename or "sheet.png")[1].lstrip(".") or "png"
    sheet_url, _ = store.write_binary("characters", contents, ext=ext, name_hint=file.filename or c.file_name or c.name or "sheet")
    st = store.load_state()
    rec = {"id": store.new_id("char"), "name": c.name, "description": c.description, "sheet_url": sheet_url, "source": "upload", "created": store.now()}
    st["characters"].append(rec)
    store.save_state(st)
    return rec


class GenerateIn(BaseModel):
    prompt: str = ""
    size: Optional[str] = None
    quality: Optional[str] = None

@app.post("/api/generate")
def api_generate(g: GenerateIn):
    st = store.load_state()
    client = get_image_client()
    final_prompt = g.prompt or pipeline.build_sequence_prompt(st.get("master_prompt", ""), st.get("style_frames", []), st.get("characters", []), "")
    try:
        img = client.generate(final_prompt, size=g.size or config.DEFAULT_SIZE, quality=g.quality or config.DEFAULT_QUALITY)
    except Exception as e:
        raise HTTPException(500, f"generate failed: {e}")
    rec = {"id": store.new_id("frame"), "url": store.write_image("images", img), "prompt": final_prompt, "created": store.now()}
    st.setdefault("generated_images", []).append(rec)
    store.save_state(st)
    return rec
