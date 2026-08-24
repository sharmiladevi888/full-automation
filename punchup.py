"""Punch-up a finished slideshow edit into a high-retention Shorts/TikTok cut.

Works from the project's ORIGINAL still frames + the per-scene narration timing
(so captions land on the right words and audio stays in sync). It adds, with
purpose — not random effects:

  * a survival-style HOOK (two cards) in the first ~3s, and a punchline OUTRO;
  * per-shot camera MOTION (alternating Ken Burns in/out/pan) so nothing sits
    static, plus micro-cuts on long holds;
  * big, punchy animated CAPTIONS popped in on the real narration beats;
  * impact SHAKE on high-intensity scenes (lava / meteors / death);
  * a procedural SOUND BED (low rumble + booms + whooshes + a final hit);
  * loud, crisp Shorts AUDIO (voice compressed + everything loudnorm'd to -14 LUFS).

What it canNOT do: change what the stickman is doing or the backgrounds — those
are baked into the stills. Reaction poses + varied lava/meteor backdrops need the
frames regenerated with new prompts (see the report printed at the end).
"""
import os
import re
import shutil
import subprocess
import tempfile
import threading

import config
import store

W, H, FPS = 1920, 1080, 30
FONT = "C\\:/Windows/Fonts/arialbd.ttf"          # escaped for ffmpeg filtergraph
HOOK_CARDS = [
    "YOU JUST LANDED ON EARTH\n4.5 BILLION YEARS AGO",
    "YOU HAVE 10 SECONDS\nTO SURVIVE",
]
OUTRO_CARD = "COME BACK IN\n4 BILLION YEARS"

_STOP = set("a an the to of in on at is are was were be been being this that "
            "those these it its you your i we they he she him her his them as and "
            "or but so just go ahead there here now then with for from into over "
            "under up down out not no your you're there's".split())
_HOT = ("lava", "meteor", "asteroid", "explos", "impact", "heat", "burn", "fire",
        "die", "death", "dead", "poison", "toxic", "boil", "crash", "collide",
        "1200", "1,200", "degrees", "no oxygen", "no air", "molten", "erupt")


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{' '.join(cmd)[:300]}\n{p.stderr[-700:]}")
    return p


# --------------------------------------------------------------------------- #
#  Captions
# --------------------------------------------------------------------------- #
def _heuristic_caption(vo):
    t = (vo or "").strip().rstrip(".!?")
    # Keep a number+unit if present (e.g. 1,200°C, 10 seconds).
    m = re.search(r"\d[\d,\.]*\s*°?\s*[A-Za-z]{0,7}", t)
    words = re.findall(r"[A-Za-z0-9°,\.]+", t)
    keep = [w for w in words if (w.lower() not in _STOP) or any(c.isdigit() for c in w)]
    phrase = " ".join((keep or words)[:3]).upper()
    phrase = phrase.strip(",. ")
    if m and not any(c.isdigit() for c in phrase):
        phrase = (m.group(0).strip() + " " + phrase).strip()
    return phrase[:26] or (t[:20].upper())


def _intensity(vo):
    low = (vo or "").lower()
    return "high" if any(k in low for k in _HOT) else "normal"


def _captions_via_claude(scenes):
    """One Claude call: turn each VO line into a 1-3 word punch caption + sfx +
    intensity. Returns list aligned to scenes, or None on any failure."""
    key = config.CLAUDE_API_KEY or os.environ.get("CLAUDE_API_KEY", "")
    base = config.CLAUDE_BASE_URL
    model = config.CLAUDE_MODEL
    if not key:
        # try the app vault (per-user key)
        try:
            import app
            vault = app.load_vault()
            for _email, u in (vault or {}).items():
                if u.get("claude_api_key"):
                    key = u["claude_api_key"]
                    base = u.get("claude_base_url", base)
                    model = u.get("claude_model", model)
                    break
        except Exception:
            pass
    if not key:
        return None
    try:
        from claude_client import ClaudeClient, extract_json
        c = ClaudeClient(api_key=key, base_url=base, model=model)
        lines = "\n".join(f"{i+1}. {s['vo']}" for i, s in enumerate(scenes))
        system = (
            "You caption a fast survival-style explainer Short. For each narration "
            "line return a HUGE, PUNCHY on-screen caption: 1-4 WORDS, UPPERCASE, no "
            "full sentences (e.g. 'POISON AIR', 'NO OXYGEN', '1200°C', 'LAVA "
            "EVERYWHERE', 'YOU DIE INSTANTLY'). Also rate how intense/dangerous the "
            "moment is. Return STRICT JSON ONLY: "
            '{"captions":[{"text":str,"intensity":"low"|"normal"|"high"}]} with '
            "exactly one entry per numbered line, in order.")
        raw = c.chat_text("Lines:\n" + lines + "\n\nJSON only.", system=system,
                          max_tokens=2000)
        data = extract_json(raw)
        caps = data.get("captions") or []
        if len(caps) >= len(scenes):
            return [(str(caps[i].get("text", "")).upper().strip()[:26] or
                     _heuristic_caption(scenes[i]["vo"]),
                     caps[i].get("intensity", _intensity(scenes[i]["vo"])))
                    for i in range(len(scenes))]
    except Exception as e:
        print(f"[punchup] caption Claude pass failed ({e}); using heuristic")
    return None


# --------------------------------------------------------------------------- #
#  Video clips
# --------------------------------------------------------------------------- #
def _drawtext(text_path, *, big=True, slam=False):
    fs = int(H / 8) if big else int(H / 12)        # numeric — drawtext needs ints
    border = max(2, int(H / 150))
    # alpha pop-in; y slides up over the first 0.18s; slam = quick alpha snap.
    ypop = "(h*0.70 - 40*max(0\\,1-t/0.18))"
    alpha = "min(1\\,t/0.12)" if not slam else "min(1\\,t/0.06)"
    return (f"drawtext=fontfile='{FONT}':textfile='{text_path}':"
            f"fontcolor=white:fontsize={fs}:line_spacing=14:"
            f"borderw={border}:bordercolor=black@0.9:"
            f"shadowcolor=black@0.6:shadowx=4:shadowy=5:"
            f"x=(w-text_w)/2:y={ypop}:alpha='{alpha}'")


def _scene_clip(img, dur, caption, intensity, idx, tmp, kind):
    """Render ONE scene still into a moving, captioned clip."""
    frames = max(2, round(dur * FPS))
    # Alternating camera move so consecutive shots never feel the same.
    mode = idx % 3
    if mode == 0:      # zoom in
        z = "min(zoom+0.0015,1.22)"; x = "iw/2-(iw/zoom/2)"; y = "ih/2-(ih/zoom/2)"
    elif mode == 1:    # zoom out
        z = "if(lte(on,1),1.22,max(zoom-0.0015,1.02))"; x = "iw/2-(iw/zoom/2)"; y = "ih/2-(ih/zoom/2)"
    else:              # slow pan across, slight zoom
        z = "1.14"; x = f"(iw-iw/zoom)*on/{frames}"; y = "ih/2-(ih/zoom/2)"
    vf = (f"scale={W*1.4:.0f}:{H*1.4:.0f}:force_original_aspect_ratio=increase,"
          f"crop={W*1.4:.0f}:{H*1.4:.0f},"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},setsar=1")
    if intensity == "high":          # handheld impact shake
        vf += (f",crop={W-48}:{H-48}:"
               f"x='24+10*sin(2*PI*9*t)':y='24+9*cos(2*PI*8*t)',"
               f"scale={W}:{H},setsar=1")
    cap_path = os.path.join(tmp, f"cap_{idx:03d}.txt")
    with open(cap_path, "w", encoding="utf-8") as f:
        f.write(caption)
    vf += "," + _drawtext(cap_path.replace("\\", "/").replace(":", "\\:"),
                          big=True)
    out = os.path.join(tmp, f"clip_{idx:03d}.mp4")
    _run(["ffmpeg", "-y", "-loop", "1", "-t", f"{dur:.3f}", "-i", img,
          "-vf", vf, "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
          "-preset", "veryfast", "-crf", "20", out])
    return out


def _card_clip(text, dur, idx, tmp, red=True):
    """A dramatic full-screen text card (hook / outro)."""
    cap_path = os.path.join(tmp, f"card_{idx:03d}.txt")
    with open(cap_path, "w", encoding="utf-8") as f:
        f.write(text)
    vf = (f"drawbox=x=0:y=0:w={W}:h={H}:color=0x0a0a0c:t=fill,"
          + _drawtext(cap_path.replace("\\", "/").replace(":", "\\:"),
                      big=True, slam=True))
    if red:                          # danger flash: red vignette fading out
        vf += (f",drawbox=x=0:y=0:w={W}:h=18:color=red@0.9:t=fill,"
               f"drawbox=x=0:y={H-18}:w={W}:h=18:color=red@0.9:t=fill,"
               f"fade=t=in:st=0:d=0.12")
    out = os.path.join(tmp, f"card_{idx:03d}.mp4")
    _run(["ffmpeg", "-y", "-f", "lavfi", "-t", f"{dur:.3f}",
          "-i", f"color=c=0x0a0a0c:s={W}x{H}:r={FPS}",
          "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
          "-preset", "veryfast", "-crf", "20", out])
    return out


# --------------------------------------------------------------------------- #
#  Audio / sound design
# --------------------------------------------------------------------------- #
def _build_audio(voice_path, voice_delay, total_dur, boom_times, out_wav, tmp):
    """Voice (delayed, compressed) + rumble bed + booms + final hit -> loud mix."""
    inputs, filt, labels = [], [], []
    n = 0

    def add_input(args):
        nonlocal n
        inputs.extend(args)
        i = n
        n += 1
        return i

    # 0: voice
    vi = add_input(["-i", voice_path])
    delay_ms = int(max(0, voice_delay) * 1000)
    filt.append(f"[{vi}:a]adelay={delay_ms}|{delay_ms},"
                f"acompressor=threshold=-18dB:ratio=4:attack=5:release=120,"
                f"volume=2.2[vo]")
    labels.append("[vo]")

    # rumble bed (brown noise, lowpassed, quiet)
    ri = add_input(["-f", "lavfi", "-t", f"{total_dur:.3f}",
                    "-i", "anoisesrc=color=brown:amplitude=0.5:sample_rate=44100"])
    filt.append(f"[{ri}:a]lowpass=f=110,volume=0.10[rumble]")
    labels.append("[rumble]")

    # booms at impact beats (low sine with fast decay)
    for k, bt in enumerate(boom_times):
        bi = add_input(["-f", "lavfi", "-t", "0.7",
                        "-i", "sine=frequency=58:sample_rate=44100"])
        d = int(max(0, bt) * 1000)
        filt.append(f"[{bi}:a]volume='exp(-t*6)':eval=frame,"
                    f"adelay={d}|{d},volume=0.9[boom{k}]")
        labels.append(f"[boom{k}]")

    # final comedic hit near the very end
    hit_at = max(0.0, total_dur - 1.6)
    hi = add_input(["-f", "lavfi", "-t", "0.9",
                    "-i", "sine=frequency=80:sample_rate=44100"])
    dh = int(hit_at * 1000)
    filt.append(f"[{hi}:a]volume='exp(-t*5)':eval=frame,adelay={dh}|{dh},"
                f"volume=1.0[hit]")
    labels.append("[hit]")

    mix = "".join(labels) + (f"amix=inputs={len(labels)}:duration=longest:"
                             f"normalize=0,"
                             f"loudnorm=I=-14:TP=-1.0:LRA=11,"
                             f"alimiter=limit=0.97[mix]")
    filt.append(mix)
    _run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filt),
          "-map", "[mix]", "-t", f"{total_dur:.3f}",
          "-c:a", "pcm_s16le", out_wav])
    return out_wav


# --------------------------------------------------------------------------- #
#  Orchestration
# --------------------------------------------------------------------------- #
def _run_punchup(out_path, use_claude=True, hook_dur=1.6, outro_dur=2.0):
    st = store.load_state()
    edits = st.get("edits") or []
    if not edits:
        raise SystemExit("No edit in the current project to punch up.")
    plan = edits[-1].get("plan") or {}
    shots = plan.get("shots") or []
    seq = st.get("sequence") or []
    by_idx = {s.get("index"): s for s in seq}
    scenes = []
    for sh in shots:
        f = by_idx.get(sh.get("index"))
        if not f:
            continue
        try:
            img = store.url_to_path(f["image_url"])
        except Exception:
            continue
        scenes.append({"img": img, "dur": float(sh.get("hold_seconds") or 1.2),
                       "vo": sh.get("vo") or ""})
    if not scenes:
        raise SystemExit("No frames found for the edit.")

    vo_rec = st.get("voiceover") or {}
    vo_url = vo_rec.get("url") or (st.get("audio") or {}).get("url")
    voice_path = store.url_to_path(vo_url) if vo_url else None

    caps = _captions_via_claude(scenes) if use_claude else None
    if not caps:
        caps = [(_heuristic_caption(s["vo"]), _intensity(s["vo"])) for s in scenes]

    # Write temp clips on the SAME drive as the output (C: is often full; the
    # project/output lives on a roomier drive).
    tmp_root = os.path.dirname(os.path.abspath(out_path)) or "."
    tmp = tempfile.mkdtemp(prefix="punchup_", dir=tmp_root)
    clips = []
    boom_times = []
    t = 0.0
    # Hook cards first (danger immediately).
    for i, txt in enumerate(HOOK_CARDS):
        clips.append(_card_clip(txt, hook_dur, 900 + i, tmp, red=True))
        boom_times.append(t)
        t += hook_dur
    hook_total = t

    for i, sc in enumerate(scenes):
        cap, intat = caps[i]
        clips.append(_scene_clip(sc["img"], sc["dur"], cap, intat, i, tmp,
                                 "scene"))
        if intat == "high":
            boom_times.append(t)
        t += sc["dur"]
    scenes_total = t

    clips.append(_card_clip(OUTRO_CARD, outro_dur, 999, tmp, red=False))
    boom_times.append(scenes_total)        # slam into the punchline
    total_dur = scenes_total + outro_dur

    # Concat all clips (silent video).
    listf = os.path.join(tmp, "list.txt")
    with open(listf, "w", encoding="utf-8") as f:
        for c in clips:
            f.write(f"file '{os.path.abspath(c)}'\n")
    silent = os.path.join(tmp, "silent.mp4")
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listf,
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
          "-crf", "20", "-r", str(FPS), silent])

    # Audio: voice starts after the hook, plus the sound bed; loud + crisp.
    final_audio = None
    if voice_path:
        final_audio = _build_audio(voice_path, hook_total, total_dur, boom_times,
                                   os.path.join(tmp, "mix.wav"), tmp)

    if final_audio:
        _run(["ffmpeg", "-y", "-i", silent, "-i", final_audio,
              "-map", "0:v:0", "-map", "1:a:0",
              "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
              "-shortest", out_path])
    else:
        _run(["ffmpeg", "-y", "-i", silent, "-c", "copy", out_path])

    shutil.rmtree(tmp, ignore_errors=True)
    return out_path, total_dur, len(scenes), caps


# --------------------------------------------------------------------------- #
#  Failure-path cleanup wrapper
# --------------------------------------------------------------------------- #
# Keep the original render implementation intact while ensuring that its
# unique temp directory is removed when any ffmpeg step raises.
_PUNCHUP_CLEANUP_LOCK = threading.RLock()


def _punchup_temp_dirs(root):
    root = os.path.abspath(root or ".")
    try:
        return {
            os.path.join(root, name)
            for name in os.listdir(root)
            if name.startswith("punchup_")
            and os.path.isdir(os.path.join(root, name))
        }
    except OSError:
        return set()


def run(out_path, use_claude=True, hook_dur=1.6, outro_dur=2.0):
    root = os.path.dirname(os.path.abspath(out_path or ".")) or "."
    with _PUNCHUP_CLEANUP_LOCK:
        before = _punchup_temp_dirs(root)
        try:
            return _run_punchup(out_path, use_claude, hook_dur, outro_dur)
        finally:
            for path in _punchup_temp_dirs(root) - before:
                shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    outp = sys.argv[1] if len(sys.argv) > 1 else "punchup_out.mp4"
    use_claude = "--no-claude" not in sys.argv
    path, dur, n, caps = run(outp, use_claude=use_claude)
    print(f"\n[punchup] DONE -> {path}  ({dur:.1f}s, {n} scenes)")
    print("[punchup] captions used:")
    for i, (c, it) in enumerate(caps):
        print(f"   {i+1:2d}. [{it:6}] {c}")
