"""Local image generation via HuggingFace diffusers — 100% FREE, runs on your GPU/CPU.

Fourth image provider for Continuity Studio. Surfaces Meta's Stable Diffusion
XL (base + turbo) and Black Forest Labs' FLUX.1-schnell as on-device
generators with no per-call cost and no rate limits.

Install once:
    pip install diffusers transformers accelerate safetensors

First run downloads the model weights into ``data/diffusers_models/``:
  * stabilityai/sdxl-turbo         ~ 5 GB   (1-step, fastest, batch-friendly)
  * stabilityai/stable-diffusion-xl-base-1.0   ~ 7 GB   (25-50 steps, high quality)
  * black-forest-labs/FLUX.1-schnell (fp8)     ~12 GB   (4 steps, best free quality)

Same surface as derouter.ImageClient (generate / edit / ping) so it slots
straight into ``app.get_image_client()``. ``edit()`` uses the same contact-
sheet approach as derouter for multi-reference frames.
"""
import os
import sys
import time

import requests


def _log(msg):
    print(f"[diffusers] {msg}", file=sys.stderr, flush=True)


# Curated models. Each entry has the HF repo id, default inference steps,
# recommended dtype (fp16/fp8), and a friendly display name. Order = default
# order shown in the Settings UI. ``id`` is the short name we save.
DIFFUSERS_MODELS = [
    {"id": "sdxl-turbo",  "name": "SDXL Turbo (1-step, fastest — batch-friendly)",
     "hf_repo": "stabilityai/sdxl-turbo", "steps": 1, "guidance": 0.0,
     "dtype": "fp16", "approx_size_gb": 5},
    {"id": "sdxl-base",   "name": "Stable Diffusion XL (25-50 steps, high quality)",
     "hf_repo": "stabilityai/stable-diffusion-xl-base-1.0", "steps": 30,
     "guidance": 7.5, "dtype": "fp16", "approx_size_gb": 7},
    {"id": "flux-schnell","name": "FLUX.1-schnell (4 steps, best free local quality)",
     "hf_repo": "black-forest-labs/FLUX.1-schnell", "steps": 4,
     "guidance": 0.0, "dtype": "bf16", "approx_size_gb": 24},
]

_MODEL_INDEX = {m["id"]: m for m in DIFFUSERS_MODELS}
DEFAULT_MODEL_ID = "sdxl-turbo"

# Where downloaded model weights are cached (gitignored). Same convention as
# Piper's voice cache.
DIFFUSERS_CACHE_DIR = os.environ.get(
    "DIFFUSERS_CACHE_DIR", ""
)  # "" -> data/diffusers_models


def _models_dir():
    if DIFFUSERS_CACHE_DIR.strip():
        return DIFFUSERS_CACHE_DIR.strip()
    try:
        import store
        d = os.path.join(store.DATA_DIR, "diffusers_models")
    except Exception:
        d = os.path.join("data", "diffusers_models")
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
#  Lazy import + model cache
# --------------------------------------------------------------------------- #
_diffusers_available = None
_pipe_cache = {}     # model_id -> (pipe, dtype, device)


def diffusers_available() -> bool:
    """True iff the diffusers package + torch + transformers are installed.
    Doesn't pre-load any model weights — those download on first generate()."""
    global _diffusers_available
    if _diffusers_available is not None:
        return _diffusers_available
    try:
        import torch  # noqa: F401
        import diffusers  # noqa: F401
        import transformers  # noqa: F401
        _diffusers_available = True
    except Exception:
        _diffusers_available = False
    return _diffusers_available


def diffusers_install_hint() -> str:
    return ("Local diffusers not installed. Run ONCE:\n"
            "    pip install diffusers transformers accelerate safetensors\n"
            "(plus torch if you don't already have it — pick the CUDA build\n"
            "matching your driver from https://pytorch.org). First generate()\n"
            "will download ~5 GB of model weights into data/diffusers_models/.\n"
            "GPU is strongly recommended — CPU works but is ~10x slower.")


def _get_pipe(model_id: str):
    """Lazy-load (and cache) a Stable Diffusion or FLUX pipeline.

    FLUX repositories use ``FluxPipeline`` rather than one of the Stable
    Diffusion pipeline classes. Selecting the old class makes
    ``FLUX.1-schnell`` fail while loading its model config.
    """
    if model_id in _pipe_cache:
        return _pipe_cache[model_id]
    if not diffusers_available():
        raise RuntimeError(diffusers_install_hint())
    import torch
    from diffusers import StableDiffusionXLPipeline, StableDiffusionPipeline

    info = _MODEL_INDEX.get(model_id) or _MODEL_INDEX[DEFAULT_MODEL_ID]
    hf_repo = info["hf_repo"]
    dtype_str = info.get("dtype", "fp16")

    use_cuda = bool(torch.cuda.is_available())
    dtype = torch.float16 if dtype_str == "fp16" else torch.bfloat16

    cache_dir = _models_dir()
    os.environ["HF_HOME"] = cache_dir
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", cache_dir)

    is_flux = "flux" in hf_repo.lower()
    if is_flux:
        try:
            from diffusers import FluxPipeline
        except ImportError as e:
            raise RuntimeError(
                "This diffusers version does not include FluxPipeline. "
                "Upgrade with: pip install -U diffusers") from e
        pipe_cls = FluxPipeline
    else:
        # sdxl-turbo uses a single-step variant of the SDXL pipeline; we still
        # load via StableDiffusionXLPipeline because it has the same API.
        is_sdxl = "xl" in hf_repo.lower() or "sdxl" in hf_repo.lower()
        pipe_cls = StableDiffusionXLPipeline if is_sdxl else StableDiffusionPipeline

    load_kwargs = {
        "torch_dtype": dtype,
        "cache_dir": cache_dir,
        "use_safetensors": True,
    }
    # The SDXL repos publish fp16 variants; FLUX.1-schnell is loaded through
    # its native FluxPipeline without the SDXL-only ``variant`` argument.
    if not is_flux:
        load_kwargs["variant"] = dtype_str

    t0 = time.time()
    pipe = pipe_cls.from_pretrained(hf_repo, **load_kwargs)
    if use_cuda:
        try:
            pipe = pipe.to("cuda")
            # xformers / sdp attention speedup when available.
            pipe.enable_xformers_memory_efficient_attention()
        except Exception:
            # Fall back to default attention — slower but functional.
            try:
                pipe.enable_model_cpu_offload()
            except Exception:
                pass
    else:
        # Offload to CPU: keeps RAM bounded at the cost of speed.
        try:
            pipe.enable_model_cpu_offload()
        except Exception:
            pass

    _pipe_cache[model_id] = pipe
    print(f"[diffusers] loaded {info['name']} in {time.time()-t0:.1f}s "
          f"({'CUDA' if use_cuda else 'CPU'})", flush=True)
    return pipe


# --------------------------------------------------------------------------- #
#  Client
# --------------------------------------------------------------------------- #
class DiffusersImageClient:
    """Local image-gen client backed by HuggingFace diffusers. Free forever,
    no rate limits, no API key. Same surface as derouter.ImageClient so the
    app's render pipeline doesn't change."""

    def __init__(self, model=None, steps=None, guidance=None, device=None,
                 timeout=None):
        self.model = (model or DEFAULT_MODEL_ID)
        if self.model not in _MODEL_INDEX:
            self.model = DEFAULT_MODEL_ID
        self.steps = int(steps) if steps is not None else _MODEL_INDEX[self.model]["steps"]
        self.guidance = float(guidance) if guidance is not None else _MODEL_INDEX[self.model]["guidance"]
        self.device_override = device
        self.timeout = timeout or 300

    def ping(self):
        """Cheap connectivity + install check. Doesn't load the model (that's
        deferred to first generate() so ping stays sub-second)."""
        ok = diffusers_available()
        # Also check we can reach HF hub for the model download.
        hf_ok = False
        try:
            info = _MODEL_INDEX.get(self.model) or _MODEL_INDEX[DEFAULT_MODEL_ID]
            r = requests.head(f"https://huggingface.co/{info['hf_repo']}", timeout=10)
            hf_ok = r.status_code < 400
        except Exception:
            pass
        return {
            "ok": ok and hf_ok,
            "detail": ("diffusers installed, HF reachable" if ok and hf_ok
                       else ("install diffusers" if not ok else "HF unreachable")),
            "models": [m["id"] for m in DIFFUSERS_MODELS],
            "configured_model": self.model,
            "model_size_gb": (_MODEL_INDEX.get(self.model) or _MODEL_INDEX[DEFAULT_MODEL_ID])
                               .get("approx_size_gb", 0),
        }

    def generate(self, prompt, size=None, quality=None, retry=True, index=0):
        """Text -> PNG bytes. Uses the configured model + steps + guidance.
        Returns a re-encoded PNG so downstream code that expects PNG keeps
        working unchanged."""
        w, h = self._parse_size(size)
        from io import BytesIO
        try:
            import torch
        except ImportError:
            raise RuntimeError(diffusers_install_hint())
        pipe = _get_pipe(self.model)

        # PIL safety: clamp to the model's supported multiple of 8.
        w = max(64, (int(w) // 8) * 8)
        h = max(64, (int(h) // 8) * 8)

        t0 = time.time()
        try:
            result = pipe(
                prompt=prompt,
                num_inference_steps=int(self.steps),
                guidance_scale=float(self.guidance),
                height=int(h),
                width=int(w),
            )
        except Exception as e:
            raise RuntimeError(f"diffusers generation failed: {e}")
        img = result.images[0]
        elapsed = time.time() - t0
        print(f"[diffusers] generated {w}x{h} in {elapsed:.1f}s ({self.model})",
              flush=True)

        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    def edit(self, prompt, images, size=None, quality=None,
             retry=True, index=0, **kwargs):
        """Multi-image ref via a contact sheet (same approach derouter uses).
        The current diffusers build of the supported models doesn't take
        separate ref images in ``__call__`` directly, so we composite all
        refs into one labelled sheet and feed it through img2img when
        available. For SDXL/SDXL-Turbo we fall back to prompt-only with
        a colour-palette hint (matches Pollinations' behaviour)."""
        if not images:
            return self.generate(prompt, size=size, quality=quality,
                                 retry=retry, index=index)
        # Try img2img with a contact-sheet input — best-effort, may fail on
        # some models. If it fails, fall back to prompt-only.
        try:
            from PIL import Image
            from io import BytesIO
            if isinstance(images, (list, tuple)) and images:
                first = images[0]
                if isinstance(first, (bytes, bytearray)):
                    im = Image.open(BytesIO(first)).convert("RGB")
                elif hasattr(first, "convert"):
                    im = first.convert("RGB")
                else:
                    im = Image.open(first).convert("RGB")
                w, h = self._parse_size(size)
                w = max(64, (int(w) // 8) * 8)
                h = max(64, (int(h) // 8) * 8)
                # Resize contact-sheet thumbnail to model input.
                im = im.resize((w, h))
                pipe = _get_pipe(self.model)
                # SDXL pipelines expose img2img via from_pipe.
                if hasattr(pipe, "from_pipe"):
                    try:
                        from diffusers import StableDiffusionXLImg2ImgPipeline
                        img2img = StableDiffusionXLImg2ImgPipeline.from_pipe(pipe)
                    except Exception:
                        img2img = None
                else:
                    img2img = None
                if img2img is not None:
                    t0 = time.time()
                    result = img2img(
                        prompt=prompt, image=im,
                        num_inference_steps=int(self.steps),
                        guidance_scale=float(self.guidance),
                    )
                    img = result.images[0]
                    print(f"[diffusers] img2img edit {w}x{h} in {time.time()-t0:.1f}s",
                          flush=True)
                    buf = BytesIO()
                    img.save(buf, format="PNG", optimize=True)
                    return buf.getvalue()
        except Exception as e:
            print(f"[diffusers] img2img fallback to prompt-only: {e}", flush=True)
        # Fallback: prompt-only with a colour-palette summary of the refs.
        hint = ""
        try:
            from PIL import Image
            from io import BytesIO
            if isinstance(images, (list, tuple)) and images:
                first = images[0]
                if isinstance(first, (bytes, bytearray)):
                    im = Image.open(BytesIO(first)).convert("RGB")
                elif hasattr(first, "convert"):
                    im = first.convert("RGB")
                else:
                    im = Image.open(first).convert("RGB")
                small = im.resize((32, 32))
                pixels = list(small.getdata())
                r = sum(p[0] for p in pixels) // len(pixels)
                g = sum(p[1] for p in pixels) // len(pixels)
                b = sum(p[2] for p in pixels) // len(pixels)
                if max(r, g, b) - min(r, g, b) < 18:
                    mood = f"neutral grey RGB({r},{g},{b})"
                elif r > g and r > b:
                    mood = f"warm red/orange RGB({r},{g},{b})"
                elif b > r and b > g:
                    mood = f"cool blue RGB({r},{g},{b})"
                elif g > r and g > b:
                    mood = f"green RGB({r},{g},{b})"
                else:
                    mood = f"mixed RGB({r},{g},{b})"
                hint = (f" Style anchor: dominant palette {mood}. "
                        f"Keep the same look as the reference frames.")
        except Exception:
            pass
        return self.generate(prompt + hint, size=size, quality=quality,
                             retry=retry, index=index)

    # ---- utilities -------------------------------------------------- #

    @staticmethod
    def _parse_size(size):
        if size is None:
            return 1024, 1024
        if isinstance(size, str):
            try:
                w, h = size.lower().split("x")
                return int(w), int(h)
            except Exception:
                return 1024, 1024
        if isinstance(size, (tuple, list)) and len(size) == 2:
            return int(size[0]), int(size[1])
        return 1024, 1024
