"""Deterministic near-duplicate scene guard for the shot list.

WHY THIS EXISTS
---------------
Repetitive output ("same scene with different changes") is a PLANNING problem,
not an image-feed problem: the shot-list LLM emits samey beats. Prompt rules
help (see TRACKING.md, claude_client.py hardening) but the model still slips
duplicates through. This module catches them deterministically AFTER the shot
list is parsed, so you can rewrite only the offending scenes.

Standalone, stdlib-only. See TRACKING.md for the one-time integration.
"""
import re
import sys

_STOP = {
    "the", "a", "an", "and", "with", "in", "on", "of", "at", "to", "is",
    "are", "same", "style", "scene", "cartoon", "comic", "web", "frame",
}


def _scene_signature(scene):
    """A coarse fingerprint of a scene: the set of salient (setting + action)
    words, order-independent and lossy so 'MAX at cafe drinking' and 'MAX at
    the cafe, drinking coffee' collide."""
    blob = f"{scene.get('setting') or ''} {scene.get('prompt') or ''}".lower()
    blob = re.sub(r"[^a-z0-9 ]", " ", blob)
    return frozenset(w for w in blob.split() if w and w not in _STOP)


def find_duplicate_scenes(scenes, threshold=0.8):
    """Return [(i, j), ...] index pairs whose scenes are near-identical.
    Similarity = Jaccard overlap of salient-word signatures; >= threshold is
    flagged as a duplicate beat that should be rewritten."""
    sigs = [_scene_signature(s) for s in scenes]
    dupes = []
    for i in range(len(sigs)):
        for j in range(i + 1, len(sigs)):
            a, b = sigs[i], sigs[j]
            if not a or not b:
                continue
            if len(a & b) / (len(a | b) or 1) >= threshold:
                dupes.append((i, j))
    return dupes


def assert_scene_diversity(scenes, threshold=0.8, hard=False):
    """Log (or raise on hard=True) when scenes repeat. Call right after parsing
    the shot list. Returns the duplicate pairs so the caller can request
    targeted rewrites of just the second index of each pair."""
    dupes = find_duplicate_scenes(scenes, threshold=threshold)
    for i, j in dupes:
        print(f"[diversity] scenes {i} and {j} are near-duplicates (same beat):"
              f"\n  #{i}: {str(scenes[i].get('prompt',''))[:80]}"
              f"\n  #{j}: {str(scenes[j].get('prompt',''))[:80]}",
              file=sys.stderr, flush=True)
    if dupes and hard:
        raise ValueError(f"{len(dupes)} duplicate scene beats detected; "
                         f"regenerate the shot list.")
    return dupes


def dedupe_indices(dupes):
    """Given duplicate pairs, return the sorted set of SECOND indices -- the
    scenes to rewrite (keep the first occurrence of each colliding beat)."""
    return sorted({j for _, j in dupes})
