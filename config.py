"""Central configuration. All values can be overridden via environment variables
(or a .env file loaded by app.py)."""
import os


def _get(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


# --- derouter / OpenAI-compatible IMAGE endpoint ---------------------------
# IMPORTANT: always use the *api-direct* host for image gen — those calls are
# long-running (15–240 s) and the regular host would 524 from Cloudflare's
# 100 s timeout. api-direct gives 600 s.
API_KEY = _get("DEROUTER_API_KEY", "")
BASE_URL = _get("DEROUTER_BASE_URL", "https://api-direct.derouter.network/openai/v1")
MODEL = _get("IMAGE_MODEL", "gpt-image-2")
# Image gen can run long (a 2K/high render was observed at ~7.5 min). The
# api-direct host allows up to 600s, so default to that ceiling rather than
# aborting a good generation early.
TIMEOUT = int(_get("REQUEST_TIMEOUT", "600"))

# --- derouter ANTHROPIC-compatible (Claude) endpoint -----------------------
# Used for: script gen, prompts-from-video (vision), edit planning (vision).
CLAUDE_API_KEY = _get("CLAUDE_API_KEY", _get("ANTHROPIC_API_KEY", ""))
CLAUDE_BASE_URL = _get("CLAUDE_BASE_URL", "https://api.derouter.network/proxy")
CLAUDE_MODEL = _get("CLAUDE_MODEL", "claude-sonnet-4-6")

# --- Direct Anthropic API (alternative to derouter for Claude) --------------
ANTHROPIC_DIRECT_API_KEY = _get("ANTHROPIC_DIRECT_API_KEY", "")
ANTHROPIC_DIRECT_BASE_URL = "https://api.anthropic.com"

# --- 9Router (local AI router / token-saver proxy) -------------------------
# Optional: route Claude calls through a locally-running 9Router instance
# (https://github.com/decolua/9router). Install with `npm i -g 9router`, run
# `9router`, connect a provider in its dashboard, then in Settings pick the
# "9Router" provider here. 9Router serves an Anthropic-compatible API at
# http://localhost:20128, so the same Anthropic SDK works unchanged — we just
# point base_url at it. Benefits: RTK token-saving (20-40%), multi-provider
# fallback (free -> cheap -> subscription) and quota tracking.
#
# NOTE: this only affects Claude/text calls (script gen, vision prompts, edit
# planning). Image generation still goes through the image provider (derouter /
# OpenRouter) — 9Router routes chat/code models, not gpt-image.
NINEROUTER_API_KEY = _get("NINEROUTER_API_KEY", "")
NINEROUTER_BASE_URL = _get("NINEROUTER_BASE_URL", "http://127.0.0.1:20128")
NINEROUTER_MODEL = _get("NINEROUTER_MODEL", "cc/claude-sonnet-4-6")
# Image generation through 9Router uses its OpenAI-compatible endpoint (note the
# trailing /v1 — the images API lives under it, unlike the Anthropic base above).
# NOTE: 9Router is built for chat/code LLMs; whether it proxies the OpenAI images
# endpoint depends on the provider/account you connect in its dashboard. If image
# gen returns an error, keep image generation on the derouter provider.
NINEROUTER_IMAGE_BASE_URL = _get("NINEROUTER_IMAGE_BASE_URL", "http://127.0.0.1:20128/v1")
NINEROUTER_IMAGE_MODEL = _get("NINEROUTER_IMAGE_MODEL", "gpt-image-2")
# When the local-router Claude route keeps stalling/502ing, the SAME call is
# rerouted to this model on the router's OpenAI endpoint. Default is empty
# (disabled) — set it ONLY to a model that is NOT rate-limited, otherwise the
# fallback just trades one failure for another (e.g. cx/gpt-5.5 hitting its
# usage cap). Pick a non-Claude model on a provider with free quota.
CLAUDE_FALLBACK_MODEL = _get("CLAUDE_FALLBACK_MODEL", "")
# Fallback 9Router model ids (provider-prefixed) used only when the live
# /v1/models fetch fails. The Settings UI loads the real list from your
# running 9Router, so this just needs sane defaults. cc/ = Claude (OAuth),
# cx/ = Codex; kr/oc/glm entries only work if those providers are connected.
NINEROUTER_MODELS = [
    "cc/claude-sonnet-4-6",
    "cc/claude-opus-4-8",
    "cc/claude-opus-4-7",
    "cc/claude-haiku-4-5-20251001",
    "cx/gpt-5.5",
    "kr/claude-sonnet-4.5",
]

# --- AgentRouter (Anthropic-compatible Claude proxy) -----------------------
# Drop-in Claude proxy — set base_url to https://agentrouter.org and auth via
# Authorization: Bearer key. Uses the same Anthropic SDK path as derouter.
AGENTROUTER_API_KEY = _get("AGENTROUTER_API_KEY", "")
AGENTROUTER_BASE_URL = _get("AGENTROUTER_BASE_URL", "https://agentrouter.org")
AGENTROUTER_MODEL = _get("AGENTROUTER_MODEL", "claude-sonnet-4-6")
AGENTROUTER_MODELS = [
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]

# --- ElevenLabs (voice-over text-to-speech) --------------------------------
# Turns the script's voice-over text into real spoken audio. Get a key at
# https://elevenlabs.io (Profile -> API key). Can also be pasted in Settings.
ELEVENLABS_API_KEY = _get("ELEVENLABS_API_KEY", "")
ELEVENLABS_BASE_URL = _get("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io/v1")
# Default voice id (public "Rachel"). The Edit tab lists your account's voices.
ELEVENLABS_VOICE_ID = _get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVENLABS_MODEL = _get("ELEVENLABS_MODEL", "eleven_multilingual_v2")

# --- Xiaomi MiMo TTS (free/cheap voice-over alternative to ElevenLabs) ------
# Speech synthesis v2.5. Get a key at https://mimo.mi.com (Console). Can also be
# pasted in Settings. Returns WAV audio; no character-level timestamps.
MIMO_API_KEY = _get("MIMO_API_KEY", "")
MIMO_BASE_URL = _get("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_MODEL = _get("MIMO_MODEL", "mimo-v2.5-tts")
MIMO_VOICE_ID = _get("MIMO_VOICE_ID", "Chloe")
# Natural-language style instruction sent as the MiMo 'user' message.
MIMO_STYLE = _get("MIMO_STYLE", "Clear, natural, engaging narration voice.")

# --- Deepgram Aura TTS (premium fast voice alternative) ---------------------
# Aura / Aura-2 text-to-speech. Get a key at https://console.deepgram.com →
# API Keys. Returns raw audio bytes (mp3 by default). Generates audio extremely
# fast (sub-second for short clips); no per-character timestamps (the caller
# falls back to word-count holds, same as MiMo).
DEEPGRAM_API_KEY = _get("DEEPGRAM_API_KEY", "")
DEEPGRAM_BASE_URL = _get("DEEPGRAM_BASE_URL", "https://api.deepgram.com/v1")
# Deepgram's "model" IS the voice — one string picks both the engine and the
# speaker (e.g. aura-2-thalia-en = Aura 2 engine, Thalia voice, English).
DEEPGRAM_MODEL = _get("DEEPGRAM_MODEL", "aura-2-thalia-en")
DEEPGRAM_VOICE_ID = _get("DEEPGRAM_VOICE_ID", "aura-2-thalia-en")
# Output container. linear16/WAV @ 24kHz is LOSSLESS — Aura's native sample
# rate — so the voice stays crisp through the pipeline's ffmpeg concat/mux
# (mp3-on-mp3 re-encodes muddy the sound and Aura's soft consonants suffer
# most). The final video mux re-encodes to AAC once, so we never ship raw WAV.
DEEPGRAM_ENCODING = _get("DEEPGRAM_ENCODING", "wav")

# --- Piper TTS (LOCAL, 100% free, runs on CPU/GPU on your machine) -----------
# Piper is an open-source ONNX neural TTS engine. Zero API cost, runs entirely
# on your machine. The first synthesize() call auto-downloads the voice model
# into data/piper_models/ and caches it forever — no setup needed.
#
# Real-world CPU perf on a modern laptop: ~1x real-time for "medium" voices,
# ~0.5x real-time for "low". Set PIPER_USE_GPU=true if you have onnxruntime-gpu
# installed and a CUDA GPU — typically 5-10x faster.
#
# No key needed. Voice id is the short name from the catalog below (e.g.
# "amy", "lessac", "kristin") and resolves to the matching ONNX bundle.
PIPER_VOICE = _get("PIPER_VOICE", "amy")
PIPER_USE_GPU = _get("PIPER_USE_GPU", "false").lower() in ("1", "true", "yes")
PIPER_LENGTH_SCALE = float(_get("PIPER_LENGTH_SCALE", "1.0"))   # >1 = slower, <1 = faster
PIPER_NOISE_SCALE = float(_get("PIPER_NOISE_SCALE", "0.667"))   # prosody variance
PIPER_NOISE_W_SCALE = float(_get("PIPER_NOISE_W_SCALE", "0.8")) # phoneme-duration variance
# Where downloaded voice models live (auto-created on first use).
PIPER_MODELS_DIR = _get("PIPER_MODELS_DIR", "")                  # "" -> data/piper_models
# Recover word/char timestamps from the synthesized audio by running local
# Whisper on it. Yields frame-accurate A/V sync identical to ElevenLabs at
# $0 cost (uses faster-whisper if installed). Disable on slow machines
# where synthesis speed matters more than cut precision.
PIPER_USE_WHISPER_FOR_TIMING = _get("PIPER_USE_WHISPER_FOR_TIMING", "true").lower() in (
    "1", "true", "yes",
)

# --- default render settings -----------------------------------------------
DEFAULT_SIZE = _get("DEFAULT_SIZE", "1536x1024")
# "medium" by default: ~half the per-image cost of "high", visually near-identical
# for flat illustration styles, and low derouter wallet balances can't reserve
# "high" edit slots (402 wallet-balance). Set DEFAULT_QUALITY=high in .env to
# restore maximum quality when the wallet is topped up.
DEFAULT_QUALITY = _get("DEFAULT_QUALITY", "medium")

# Whether the image proxy supports multiple reference images on /images/edits
# via the `image[]` multipart field.  The derouter docs only document a single
# `image` field, so the default is False: we composite all refs into one
# contact-sheet image and send it as the single documented `image` field.
# Flip this on ONLY if you've verified your proxy accepts `image[]` repeated
# fields — otherwise edits with multiple references will fail.
MULTI_IMAGE_EDIT = _get("MULTI_IMAGE_EDIT", "false").lower() == "true"

# --- image generation queue / rate-limit handling --------------------------
# The image endpoint (gpt-image via derouter / OpenAI) enforces a tokens-per-
# minute rate limit. Firing a whole batch at once trips `rate_limit_exceeded`.
# These settings drive image_queue.py: a controlled queue + exponential backoff
# + a global cooldown so we never spam the API or retry in a storm.
#
#   IMAGE_MAX_CONCURRENCY        how many image requests may be in flight at once
#   IMAGE_REQUEST_DELAY_MS       min gap between starting two image requests
#   IMAGE_MAX_RETRIES            attempts per prompt before it is marked failed
#   IMAGE_BACKOFF_BASE_MS        base for exponential backoff on server errors
#   IMAGE_BACKOFF_MAX_MS         ceiling for a single backoff wait
#   IMAGE_RATE_LIMIT_COOLDOWN_MS how long the WHOLE queue pauses after a 429
def _int(name, default):
    try:
        return int(float(_get(name, str(default))))
    except (TypeError, ValueError):
        return default


IMAGE_MAX_CONCURRENCY = max(1, _int("IMAGE_MAX_CONCURRENCY", 2))
IMAGE_REQUEST_DELAY_MS = max(0, _int("IMAGE_REQUEST_DELAY_MS", 50))
IMAGE_MAX_RETRIES = max(0, _int("IMAGE_MAX_RETRIES", 6))
IMAGE_BACKOFF_BASE_MS = max(100, _int("IMAGE_BACKOFF_BASE_MS", 1500))
IMAGE_BACKOFF_MAX_MS = max(1000, _int("IMAGE_BACKOFF_MAX_MS", 30000))
# Wait after a rate-limit (429) before the queue resumes. The TPM window is ~60s,
# but a fixed 65s overshoots when the limit is hit mid-window. We use a shorter
# pause and simply re-poll (silently) — this self-tunes to how fast tokens free
# up and recovers far quicker. Raise it if you still see frequent retries.
IMAGE_RATE_LIMIT_COOLDOWN_MS = max(1000, _int("IMAGE_RATE_LIMIT_COOLDOWN_MS", 18000))

# ffmpeg: frames-per-second to sample when splitting an uploaded video.
FRAME_FPS = float(_get("FRAME_FPS", "1"))

# Where generated assets + project state live.
DATA_DIR = _get("DATA_DIR", "data")

SUPPORTED_SIZES = [
    "1920x1080", "1024x1024", "1024x1536", "1536x1024", "2048x2048", "3840x2160", "auto",
]
SUPPORTED_QUALITIES = ["low", "medium", "high", "auto"]

# How many source-video style frames to feed as art-style anchors. Character
# sheets and scene frames both pull up to this many "STYLE REF" images from the
# pinned style_frames. More frames = a fuller read of the source look (palette,
# line weight, lighting variation, character design). Raise via STYLE_REF_COUNT
# in .env. gpt-image-2 handles 6-8 refs well; the contact-sheet fallback packs
# them into one grid so there's no hard wire-format cap.
STYLE_REF_COUNT = int(_get("STYLE_REF_COUNT", "6"))

# CHARACTER-BLEED GUARD. Style frames are whole frames from the SOURCE video and
# therefore usually CONTAIN that video's own characters. Identity is protected by
# (a) putting the CHARACTER sheets FIRST in the ref list so they lead the grid,
# and (b) the strengthened STYLE-REF captions + prompt text that tell the model
# to copy art style ONLY and ignore people shown in style frames. We deliberately
# keep a HEALTHY number of style frames so the art-style read stays strong —
# over-cutting them (to 2-3) starves the style copy and the look drifts off the
# source. With a character sheet present we trim only modestly (it already
# carries the style); with none we use the full STYLE_REF_COUNT. Tune via .env.
STYLE_FRAMES_WITH_CHARS = int(_get("STYLE_FRAMES_WITH_CHARS", "4"))
STYLE_FRAMES_NO_CHARS = int(_get("STYLE_FRAMES_NO_CHARS", "6"))

# Hard ceiling on how many reference images go into a SINGLE image-edit call
# (style frames + character sheets + previous frame, combined). Anchors the
# upper bound so we never overflow the model/proxy. gpt-image-2 / derouter
# accept many via repeated image[] fields; the OpenRouter fallback and the
# contact-sheet compositor both honour this. Raise via MAX_REF_IMAGES in .env.
MAX_REF_IMAGES = int(_get("MAX_REF_IMAGES", "10"))

AUTH_REQUIRED = _get("AUTH_REQUIRED", "false").lower() in ("1", "true", "yes")
SESSION_SECRET = _get("SESSION_SECRET", "continuity-studio-default-session-key")

# --- Google OAuth (YouTube upload + Drive export) ---
GOOGLE_CLIENT_ID = _get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = _get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = _get("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback")

CLAUDE_MODELS = [
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]

# --- OpenAI direct (alternative AI provider) ---------------------------------
OPENAI_API_KEY = _get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = _get("OPENAI_BASE_URL", "https://api.openai.com/v1")

# --- Google Gemini (AI Studio)
#     YouTube analysis and prompting. Uses Google's OpenAI-COMPATIBLE endpoint
#     so it rides the existing OpenAI chat path in claude_client (_msg_openai).
#     NOTE: free tier covers text+vision; image-gen (Nano Banana / Imagen) and
#     reliable TTS need billing enabled on the Google account. Image generation
#     stays on the image provider (derouter) regardless.
GEMINI_API_KEY = _get("GEMINI_API_KEY", "")
GEMINI_BASE_URL = _get("GEMINI_BASE_URL",
                       "https://generativelanguage.googleapis.com/v1beta/openai")
GEMINI_MODEL = _get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_MODELS = [
    "gemini-2.5-flash",         # best value: fast, 1M ctx, vision — default
    "gemini-2.5-flash-lite",    # cheapest / fastest
    "gemini-2.5-pro",           # highest quality text
    "gemini-flash-latest",
    "gemini-pro-latest",
]

# --- TokenBay (image generation via chat completions — Gemini Flash Image) --
# TokenBay serves an OpenAI-compatible /v1/chat/completions endpoint.
# Models like gemini-2.5-flash-image return base64 PNG images inline in the
# assistant message content (data URL), same pattern as OpenRouter chat-image.
TOKENBAY_API_KEY = _get("TOKENBAY_API_KEY", "")
TOKENBAY_BASE_URL = _get("TOKENBAY_BASE_URL", "https://api.tokenbay.com/v1")
TOKENBAY_MODEL = _get("TOKENBAY_MODEL", "gemini-2.5-flash-image")
TOKENBAY_TIMEOUT = int(_get("TOKENBAY_TIMEOUT", "600"))
TOKENBAY_MODELS = [
    "gemini-2.5-flash-image",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
]

# --- DashScope / Qwen Cloud (Wan 2.7 Image Pro — Alibaba) -------------------
# DashScope ( Alibaba Cloud's AI service) serves the wan2.7-image-pro model
# via a multimodal-generation endpoint that returns base64 image(s) in the
# response.  Supports enable_sequential (multi-image consistency), n (batch
# count), and size (2K etc).  Get a key at https://dashscope.aliyun.com
# (Console → API Key).  The intl endpoint is used by default.
DASHSCOPE_API_KEY = _get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = _get("DASHSCOPE_BASE_URL",
                          "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation")
DASHSCOPE_MODEL = _get("DASHSCOPE_MODEL", "wan2.7-image-pro")
DASHSCOPE_TIMEOUT = int(_get("DASHSCOPE_TIMEOUT", "600"))
DASHSCOPE_MODELS = [
    "wan2.7-image-pro",
    "wan2.5-image-pro",
    "wan2.7-image",
    "wan2.5-image",
    "qwen-image-2.0-pro",
    "qwen-image-2.0",
    "qwen-image-max",
    "qwen-image-plus",
]
# Bulk API keys: comma-separated list of keys that get round-robined to
# spread rate-limit load across multiple DashScope accounts.  Set in .env as
# DASHSCOPE_API_KEYS=key1,key2,key3  (also used for derouter/openrouter/tokenbay).
DASHSCOPE_API_KEYS = [k.strip() for k in _get("DASHSCOPE_API_KEYS", "").split(",") if k.strip()]

# Bulk API keys for other providers (comma-separated, round-robined).
DEROUTER_API_KEYS = [k.strip() for k in _get("DEROUTER_API_KEYS", "").split(",") if k.strip()]
OPENROUTER_API_KEYS = [k.strip() for k in _get("OPENROUTER_API_KEYS", "").split(",") if k.strip()]
TOKENBAY_API_KEYS = [k.strip() for k in _get("TOKENBAY_API_KEYS", "").split(",") if k.strip()]
PUTER_API_KEYS = [k.strip() for k in _get("PUTER_API_KEYS", "").split(",") if k.strip()]

# --- OpenRouter (alternative image generation via chat completions) ---------
OPENROUTER_API_KEY = _get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = _get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = _get("OPENROUTER_MODEL", "sourceful/riverflow-v2.5-pro:free")
OPENROUTER_TIMEOUT = int(_get("OPENROUTER_TIMEOUT", "600"))

OPENROUTER_MODELS = [
    "sourceful/riverflow-v2.5-fast:free",
    "sourceful/riverflow-v2.5-pro:free",
]

# When the primary derouter image endpoint returns an HTTP 402 billing error
# (e.g. "Slot reservation failed (wallet-balance)") and an OpenRouter image key
# is configured, automatically retry that single request on OpenRouter instead
# of failing. Defaults to True so a depleted derouter wallet doesn't halt a run
# when a free OpenRouter image model is available; set IMAGE_FALLBACK_ON_402=
# false in .env to disable and surface the billing error directly.
IMAGE_FALLBACK_ON_402 = _get("IMAGE_FALLBACK_ON_402", "true").lower() in (
    "1", "true", "yes",
)
