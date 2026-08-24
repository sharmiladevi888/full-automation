"""ffmpeg-based video assembly + scene detection.

We assemble final cuts from generated frames + uploaded audio according to an
EDL produced by Claude (see claude_client.ClaudeClient.plan_edit).
"""
import json
import math
import os
import subprocess
from typing import List, Dict, Optional



# Default ceilings (seconds) for ffmpeg/ffprobe subprocess calls so a hung
# encode can never block a request forever. Renders scale with clip count, so
# the assemble path gets a generous budget; probes are quick.
_PROBE_TIMEOUT = 30
_RENDER_TIMEOUT = 600


def _run(cmd, timeout, what):
    """Run an ffmpeg/ffprobe command with a hard timeout.

    Returns the CompletedProcess. Raises ``RuntimeError`` (never lets a
    ``TimeoutExpired`` or missing-binary error escape raw) so callers surface a
    clear message. Callers still inspect ``returncode``/``stderr`` themselves.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"{what} timed out after {timeout}s (command: {cmd[0]})"
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"{what} could not run: '{cmd[0]}' not found. Is ffmpeg installed "
            f"and on PATH?"
        )


def split_long_holds(shots: List[Dict], max_hold: float = 6.0,
                     zoom_step: float = 0.05) -> List[Dict]:
    """Keep a single image from sitting on screen too long.

    Any shot held longer than ``max_hold`` seconds is split into several shorter
    sub-shots of the SAME image, each at a slightly tighter zoom. Because each
    sub-shot is a static clip at a different framing, the boundaries read as
    subtle "micro-cuts" — the picture appears to step in rather than freeze.
    The total duration is preserved, so audio sync is untouched.
    """
    out: List[Dict] = []
    for sh in shots:
        dur = float(sh.get("duration") or 0)
        if dur <= max_hold or dur <= 0:
            out.append(sh)
            continue
        k = int(math.ceil(dur / max_hold))
        seg = dur / k
        for j in range(k):
            nsh = dict(sh)
            nsh["duration"] = round(seg, 3)
            nsh["zoom"] = round(1.0 + zoom_step * j, 3)   # 1.00, 1.05, 1.10 ...
            if j:
                nsh["note"] = (sh.get("note") or "") + f" · micro-cut {j+1}/{k}"
            out.append(nsh)
    return out


def probe_duration(path: str) -> float:
    """Return media duration in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", path,
    ]
    proc = _run(cmd, timeout=_PROBE_TIMEOUT, what="ffprobe duration")
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr[-400:]}")
    data = json.loads(proc.stdout or "{}")
    return float((data.get("format") or {}).get("duration") or 0)


def trim_silence(input_path: str, output_path: str = None,
                 threshold_db: int = -50, min_dur: float = 0.30,
                 lead_pad_ms: int = 80, trim_start: bool = False) -> str:
    """Strip trailing silence (and OPTIONALLY leading silence) from an audio
    file using ffmpeg silenceremove, then add a small ``lead_pad_ms`` of
    silence at the start so the first phoneme isn't right at t=0 (which can
    collide with the encoder pre-skip / decoder warm-up and sound like a
    clipped initial syllable). Returns the output path (overwrites in-place if
    ``output_path`` is None).

    ``trim_start`` defaults to **False** on purpose. Aura/Deepgram clips often
    open on a soft, slow-attack consonant or breath whose amplitude ramps up
    gradually; an aggressive start-trim (silenceremove start_periods=1) bites
    into that onset and the first WORD of every scene comes back clipped —
    exactly the "cut initial lines" symptom. We now keep the natural head of the
    clip and only strip dead air from the TAIL (which never carries speech) plus
    add the lead cushion. Pass ``trim_start=True`` only for a source you know
    has a long hard-silent intro.

    Threshold defaults to -50 dB (only TRUE silence, not low-amplitude soft
    consonants like S/F/H/Th) and the minimum silence window is 300 ms so we
    don't chop within natural sentence pauses. Loudness/clarity of the speech
    itself is preserved; we ONLY remove dead air at the edges.
    """
    out = output_path or (input_path + ".trimmed.mp3")
    pad_s = max(0.0, float(lead_pad_ms or 0)) / 1000.0
    parts = []
    # Start-trim is OPT-IN: by default we never touch the head of the clip so
    # the first syllable is never eaten. Tail-trim is always safe (no speech).
    if trim_start:
        parts.append(
            f"silenceremove=start_periods=1:start_duration={min_dur}:"
            f"start_threshold={threshold_db}dB:detection=peak"
        )
    parts.append(
        f"silenceremove=stop_periods=-1:stop_duration={min_dur}:"
        f"stop_threshold={threshold_db}dB:detection=peak"
    )
    af = ",".join(parts)
    # adelay puts ``pad_s`` of silence in front so the speech onset isn't on
    # sample 0. ``apad=pad_dur`` adds the same tail cushion so concat/atempo
    # don't truncate the last consonant.
    if pad_s > 0:
        delay_ms = int(round(pad_s * 1000))
        af += f",adelay={delay_ms}|{delay_ms},apad=pad_dur={pad_s}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af,
           "-c:a", "libmp3lame", "-q:a", "2", out]
    proc = _run(cmd, timeout=_RENDER_TIMEOUT, what="ffmpeg trim-silence")
    if proc.returncode != 0 or not os.path.exists(out):
        return input_path
    if not output_path:
        os.replace(out, input_path)
        return input_path
    return out


def detect_scenes(video_path: str, threshold: float = 0.4) -> List[float]:
    """Return a list of scene-change timestamps (seconds) using ffmpeg's
    scene detector. Threshold 0..1 (lower = more cuts)."""
    cmd = [
        "ffmpeg", "-i", video_path, "-filter:v",
        f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    proc = _run(cmd, timeout=_RENDER_TIMEOUT, what="ffmpeg scene-detect")
    # showinfo writes to stderr.
    times = []
    for line in proc.stderr.splitlines():
        # parse pts_time:XX.XXX
        idx = line.find("pts_time:")
        if idx >= 0:
            tail = line[idx + len("pts_time:"):]
            num = ""
            for ch in tail:
                if ch.isdigit() or ch == ".":
                    num += ch
                else:
                    break
            if num:
                try:
                    times.append(float(num))
                except Exception:
                    pass
    return times


# ffmpeg xfade transition names we expose (crossfade is an alias of "fade").
_XFADE = {"fade", "dissolve", "slideleft", "slideright", "slideup", "slidedown",
          "wipeleft", "wiperight", "circleopen", "circleclose", "radial",
          "zoomin", "smoothleft", "smoothright", "fadeblack", "fadewhite"}


def bookend_video(main_path, out_path, intro_img=None, outro_img=None,
                  dur=2.2, width=1920, height=1080, fps=30):
    """Concatenate optional silent intro/outro title cards around a finished
    video. Re-encodes via the concat filter so differing params don't matter.
    Returns out_path, or main_path if there's nothing to add."""
    if not intro_img and not outro_img:
        return main_path
    workdir = os.path.dirname(out_path) or "."
    tmp = os.path.join(workdir, "_bk_tmp")
    os.makedirs(tmp, exist_ok=True)

    def _card(img, name):
        cp = os.path.join(tmp, name)
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height},setsar=1,"
              f"fade=t=in:st=0:d=0.4,fade=t=out:st={max(0,dur-0.4):.3f}:d=0.4")
        cmd = ["ffmpeg", "-y", "-loop", "1", "-t", f"{dur:.3f}", "-i", img,
               "-f", "lavfi", "-t", f"{dur:.3f}",
               "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
               "-vf", vf, "-r", str(fps),
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
               "-crf", "20", "-c:a", "aac", "-b:a", "192k", "-shortest", cp]
        p = _run(cmd, timeout=_RENDER_TIMEOUT, what="ffmpeg title-card")
        if p.returncode != 0:
            raise RuntimeError(f"card failed: {p.stderr[-300:]}")
        return cp

    def _has_audio(path):
        r = _run(["ffprobe", "-v", "error", "-select_streams", "a",
                  "-show_entries", "stream=index", "-of", "csv=p=0", path],
                 timeout=_PROBE_TIMEOUT, what="ffprobe audio-check")
        return bool(r.stdout.strip())

    def _with_audio(path, name):
        """Guarantee an audio stream so the concat filter has [i:a] for every part."""
        if _has_audio(path):
            return path
        outp = os.path.join(tmp, name)
        cmd = ["ffmpeg", "-y", "-i", path, "-f", "lavfi",
               "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
               "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
               "-c:a", "aac", "-b:a", "192k", "-shortest", outp]
        p = _run(cmd, timeout=_RENDER_TIMEOUT, what="ffmpeg add-silent-audio")
        return outp if p.returncode == 0 else path

    parts = []
    if intro_img:
        parts.append(_card(intro_img, "intro.mp4"))
    parts.append(_with_audio(main_path, "main_a.mp4"))
    if outro_img:
        parts.append(_card(outro_img, "outro.mp4"))

    inputs = []
    for p in parts:
        inputs += ["-i", p]
    fc = ("".join(f"[{i}:v][{i}:a]" for i in range(len(parts))) +
          f"concat=n={len(parts)}:v=1:a=1[v][a]")
    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", fc,
           "-map", "[v]", "-map", "[a]",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
           "-crf", "20", "-c:a", "aac", "-b:a", "192k", out_path]
    p = _run(cmd, timeout=_RENDER_TIMEOUT, what="ffmpeg bookend-concat")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    if p.returncode != 0:
        raise RuntimeError(f"bookend concat failed: {p.stderr[-400:]}")
    return out_path


def assemble_video(
    shots: List[Dict],         # [{path:str, duration:float, note?:str}]
    audio_path: Optional[str],
    output_path: str,
    transition: str = "cut",   # cut | fade | crossfade | any xfade name
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    fit: str = "fill",         # fill = crop to fill (no bars) | pad = letterbox
    motion: bool = False,      # Ken Burns slow zoom on each still
    music_path: Optional[str] = None,
    music_volume: float = 0.18,
) -> str:
    """Stitch ``shots`` into a single MP4 with optional voice-over + music.

    ``cut``        -> concat demuxer
    ``fade``       -> per-shot fade-in/out (uniform 0.25s)
    ``crossfade`` / any xfade name -> xfade chain between adjacent shots (0.4s)
    ``motion``     -> slow Ken Burns zoom on every still
    ``music_path`` -> looped, ducked under the voice-over
    """
    if not shots:
        raise ValueError("no shots provided")
    # Decide how clips are combined.
    is_xfade = (transition == "crossfade") or (transition in _XFADE)
    xfade_name = "fade" if transition == "crossfade" else (
        transition if transition in _XFADE else "fade")

    workdir = os.path.dirname(output_path) or "."
    os.makedirs(workdir, exist_ok=True)
    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="_assemble_", dir=workdir)

    # 1. Build per-shot video clips, all normalised to the same size+fps.
    clip_paths = []
    for i, sh in enumerate(shots):
        clip = os.path.join(tmp_dir, f"clip_{i:04d}.mp4")
        dur = max(0.1, float(sh.get("duration") or 0))
        if fit == "pad":
            # letterbox: fit whole frame, black bars where aspect differs.
            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
            )
        else:
            # fill: scale up to cover, then center-crop to the exact size — no
            # black bars (best for a clean 16:9 / 9:16 slideshow).
            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1"
            )
        # Optional per-shot static zoom (used by micro-cuts): center-crop tighter
        # then scale back up, giving a fixed punched-in framing for this segment.
        zoom = float(sh.get("zoom") or 1.0)
        if zoom > 1.001:
            vf += (f",crop=iw/{zoom:.4f}:ih/{zoom:.4f},"
                   f"scale={width}:{height},setsar=1")
        if motion:
            frames = max(2, round(dur * fps))
            vf += (f",zoompan=z='min(zoom+0.0010,1.18)':"
                   f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                   f"d={frames}:s={width}x{height}:fps={fps},setsar=1")
        if transition == "fade":
            fade_d = min(0.25, dur / 4)
            vf += f",fade=t=in:st=0:d={fade_d},fade=t=out:st={max(0,dur-fade_d):.3f}:d={fade_d}"
        if motion:
            # Ken Burns: cap by OUTPUT frame count (not -t) so zoompan doesn't
            # explode the duration when fed a looped still.
            frames = max(2, round(dur * fps))
            cmd = [
                "ffmpeg", "-y", "-loop", "1", "-i", sh["path"],
                "-vf", vf, "-frames:v", str(frames), "-r", str(fps),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20",
                clip,
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-loop", "1", "-t", f"{dur:.3f}",
                "-i", sh["path"], "-vf", vf, "-r", str(fps),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20",
                clip,
            ]
        proc = _run(cmd, timeout=_RENDER_TIMEOUT, what="ffmpeg clip-render")
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg clip failed [{i}]: {proc.stderr[-500:]}")
        clip_paths.append((clip, dur))

    # 2. Combine clips.
    silent_video = os.path.join(tmp_dir, "video.mp4")
    if is_xfade and len(clip_paths) > 1:
        # Chain xfades. We rebuild via a single filter graph using the inputs.
        inputs = []
        for cp, _ in clip_paths:
            inputs += ["-i", cp]
        xfade_d_default = 0.4
        # Clamp xfade duration so it never exceeds the shortest adjacent clip —
        # prevents ffmpeg failures on fast micro-cut holds (can be as low as 0.3s).
        min_clip_dur = min(d for _, d in clip_paths)
        xfade_d = min(xfade_d_default, min_clip_dur / 2.0)
        if xfade_d < 0.05:
            xfade_d = 0  # too short for a visible transition — degrade to cut
        filtergraph = ""
        last_label = "0:v"
        cum = clip_paths[0][1]
        for i in range(1, len(clip_paths)):
            offset = max(0, cum - xfade_d)
            new_label = f"v{i}"
            filtergraph += (
                f"[{last_label}][{i}:v]xfade=transition={xfade_name}:duration={xfade_d}:"
                f"offset={offset:.3f}[{new_label}];"
            )
            last_label = new_label
            cum += clip_paths[i][1] - xfade_d
        filtergraph = filtergraph.rstrip(";")
        cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", filtergraph,
               "-map", f"[{last_label}]",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20",
               silent_video]
        proc = _run(cmd, timeout=_RENDER_TIMEOUT, what="ffmpeg xfade")
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg xfade failed: {proc.stderr[-600:]}")
    else:
        # concat demuxer
        list_path = os.path.join(tmp_dir, "list.txt")
        with open(list_path, "w") as f:
            for cp, _ in clip_paths:
                # ffmpeg concat needs forward slashes / escaped quotes
                f.write(f"file '{os.path.abspath(cp).replace(os.sep, '/')}'\n")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
               "-c", "copy", silent_video]
        proc = _run(cmd, timeout=_RENDER_TIMEOUT, what="ffmpeg concat")
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {proc.stderr[-600:]}")

    # 3. Mux audio (voice-over) and/or looped, ducked background music.
    has_vo = bool(audio_path and os.path.exists(audio_path))
    has_music = bool(music_path and os.path.exists(music_path))
    mv = max(0.0, min(1.0, float(music_volume)))
    if has_vo and has_music:
        cmd = ["ffmpeg", "-y",
               "-i", silent_video, "-i", audio_path,
               "-stream_loop", "-1", "-i", music_path,
               "-filter_complex",
               f"[1:a]volume=1[vo];[2:a]volume={mv}[mu];"
               f"[vo][mu]amix=inputs=2:duration=longest:normalize=0[a]",
               "-map", "0:v:0", "-map", "[a]",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
               "-shortest", output_path]
    elif has_vo:
        cmd = ["ffmpeg", "-y",
               "-i", silent_video, "-i", audio_path,
               "-map", "0:v:0", "-map", "1:a:0",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
               "-shortest", output_path]
    elif has_music:
        cmd = ["ffmpeg", "-y", "-i", silent_video, "-stream_loop", "-1", "-i", music_path,
               "-filter_complex", f"[1:a]volume={mv}[a]",
               "-map", "0:v:0", "-map", "[a]",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
               "-shortest", output_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", silent_video, "-c", "copy", output_path]

    proc = _run(cmd, timeout=_RENDER_TIMEOUT, what="ffmpeg mux")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg mux failed: {proc.stderr[-600:]}")

    # cleanup: unique temp dir — remove it entirely
    try:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass

    return output_path


def _stream_has_audio(path: str) -> bool:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "a",
              "-show_entries", "stream=index", "-of", "csv=p=0", path],
             timeout=_PROBE_TIMEOUT, what="ffprobe audio-check")
    return bool(r.stdout.strip())


def add_cut_clicks(video_path: str, click_path: str, cut_times: List[float],
                   volume: float = 0.30, output_path: str = None) -> str:
    """Overlay one short click sound at every cut timestamp in ``cut_times``.

    Uses a single ffmpeg pass with only two inputs: the click file is asplit
    into N copies, each adelay-ed to its cut time, then amix-ed over the
    existing track — so it scales to hundreds of cuts without hitting input
    limits. Overwrites in place when ``output_path`` is None.
    """
    times = sorted({round(float(t), 3) for t in (cut_times or [])
                    if t is not None and float(t) > 0.05})
    if not times or not os.path.exists(video_path) or not os.path.exists(click_path):
        return video_path
    out = output_path or (video_path + ".clicks.mp4")
    vol = max(0.02, min(1.0, float(volume)))
    n = len(times)

    parts = ["[1:a]asplit=" + str(n) + "".join(f"[c{i}]" for i in range(n)) + ";"]
    for i, t in enumerate(times):
        ms = int(t * 1000)
        parts.append(f"[c{i}]adelay={ms}|{ms},volume={vol}[k{i}];")

    if _stream_has_audio(video_path):
        labels = "[0:a]" + "".join(f"[k{i}]" for i in range(n))
        # duration=first keeps the original track's length authoritative.
        parts.append(f"{labels}amix=inputs={n + 1}:duration=first:normalize=0[out]")
        extra = []
    else:
        labels = "".join(f"[k{i}]" for i in range(n))
        if n > 1:
            parts.append(f"{labels}amix=inputs={n}:duration=longest:normalize=0,apad[out]")
        else:
            parts.append(f"{labels}apad[out]")
        extra = ["-shortest"]

    cmd = ["ffmpeg", "-y", "-i", video_path, "-i", click_path,
           "-filter_complex", "".join(parts),
           "-map", "0:v:0", "-map", "[out]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", *extra, out]
    proc = _run(cmd, timeout=_RENDER_TIMEOUT, what="ffmpeg cut-clicks")
    if proc.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"cut-click mix failed: {proc.stderr[-500:]}")
    if output_path is None:
        os.replace(out, video_path)
        return video_path
    return out


def mix_sfx(video_path: str, sfx_list: List[Dict], output_path: str = None) -> str:
    """Overlay SFX clips at given timestamps onto an existing video.

    sfx_list: [{path:str, at_seconds:float, volume:float(0-1)}]
    """
    if not sfx_list or not os.path.exists(video_path):
        return video_path
    if output_path is None:
        base, ext = os.path.splitext(video_path)
        output_path = f"{base}_sfx{ext}"

    inputs = ["-i", video_path]
    for sfx in sfx_list:
        inputs += ["-i", sfx["path"]]

    filt_parts = []
    for i, sfx in enumerate(sfx_list):
        delay_ms = int(max(0, sfx.get("at_seconds", 0)) * 1000)
        vol = max(0, min(0.5, sfx.get("volume", 0.25)))
        filt_parts.append(f"[{i+1}:a]adelay={delay_ms}|{delay_ms},volume={vol}[sfx{i}]")

    main_audio = "[0:a]"
    all_labels = [main_audio] + [f"[sfx{i}]" for i in range(len(sfx_list))]
    filt = ";".join(filt_parts) + ";" + "".join(all_labels) + f"amix=inputs={len(all_labels)}:duration=longest:normalize=0[out]"

    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", filt,
           "-map", "0:v:0", "-map", "[out]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           output_path]
    proc = _run(cmd, timeout=_RENDER_TIMEOUT, what="ffmpeg sfx-mix")
    if proc.returncode != 0:
        raise RuntimeError(f"sfx mix failed: {proc.stderr[-500:]}")
    return output_path


# --------------------------------------------------------------------------- #
#  Failure-path cleanup wrappers
# --------------------------------------------------------------------------- #
# The original render functions clean up on their success path only. These
# wrappers track the unique temp directories created by each call and remove
# them in finally blocks as well, without deleting another active render.
import shutil as _shutil
import threading as _threading

_RENDER_CLEANUP_LOCK = _threading.RLock()


def _temp_dirs(root, prefix):
    root = os.path.abspath(root or ".")
    try:
        return {
            os.path.join(root, name)
            for name in os.listdir(root)
            if name.startswith(prefix)
            and os.path.isdir(os.path.join(root, name))
        }
    except OSError:
        return set()


def _cleanup_new_temp_dirs(root, prefix, before):
    for path in _temp_dirs(root, prefix) - before:
        _shutil.rmtree(path, ignore_errors=True)


_original_assemble_video = assemble_video


def assemble_video(*args, **kwargs):
    output_path = kwargs.get("output_path")
    if output_path is None and len(args) >= 3:
        output_path = args[2]
    root = os.path.dirname(os.path.abspath(output_path or ".")) or "."
    with _RENDER_CLEANUP_LOCK:
        before = _temp_dirs(root, "_assemble_")
        try:
            return _original_assemble_video(*args, **kwargs)
        finally:
            _cleanup_new_temp_dirs(root, "_assemble_", before)


_original_bookend_video = bookend_video


def bookend_video(*args, **kwargs):
    out_path = kwargs.get("out_path")
    if out_path is None and len(args) >= 2:
        out_path = args[1]
    root = os.path.dirname(os.path.abspath(out_path or ".")) or "."
    tmp = os.path.join(root, "_bk_tmp")
    with _RENDER_CLEANUP_LOCK:
        # This is a dedicated scratch directory, so clear any stale copy before
        # starting and always remove it after both success and failure.
        _shutil.rmtree(tmp, ignore_errors=True)
        try:
            return _original_bookend_video(*args, **kwargs)
        finally:
            _shutil.rmtree(tmp, ignore_errors=True)
