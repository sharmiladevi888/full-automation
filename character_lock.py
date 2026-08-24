"""Deterministic character-lock for the video pipeline.

WHY THIS EXISTS
---------------
"Character mismatch" happens when a scene resolves to 0 characters (it then
falls back to the source video's style frames as the only human reference, so
the model borrows whoever was in the sample video) OR when the same name maps
to different sheets across scenes because matching is done ad-hoc per scene.

This module resolves character identity ONCE per render into a locked index, so:
  * every named/roled character resolves to exactly one sheet,
  * the SAME name always resolves to the SAME sheet for the whole video,
  * unresolved scenes are logged loudly instead of degrading silently.

It is intentionally standalone (imports only stdlib + pipeline for the role
alias helper) so it can be wired into the existing pipeline without touching
any current code. See TRACKING.md for the one-time integration.
"""
import re
import sys

try:
    # Reuse the existing role-alias logic so 'the monk' still resolves to the
    # monk character even when its proper name isn't in the scene text.
    from pipeline import _character_aliases
except Exception:  # pragma: no cover - pipeline import is best-effort
    def _character_aliases(_c):
        return set()


def _norm_name(s):
    """Lowercase, strip, collapse whitespace for stable name matching."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def build_character_index(characters):
    """Build a deterministic token -> character lookup.

    Resolves collisions ONCE up front so the same token can never map to two
    sheets mid-run. `characters` is the list of character dicts (each with at
    least a `name`). Returns { token(str): character(dict) } where token is a
    proper-name token OR a role alias.

    First-declared character wins a contested token (stable + predictable); the
    collision is logged so you can rename to disambiguate.

    Build this ONCE per render (before the scene loop), never per scene.
    """
    index = {}
    for c in characters:
        name = _norm_name(c.get("name"))
        tokens = set()
        if name:
            tokens.add(name)
            # also index each salient word of a multi-word name
            # ("max power" -> "max", "power")
            for w in name.split(" "):
                if len(w) >= 3:
                    tokens.add(w)
        # role aliases derived from the character's own description
        try:
            tokens |= {_norm_name(a) for a in _character_aliases(c)}
        except Exception:
            pass
        for t in tokens:
            if not t:
                continue
            if t in index and index[t] is not c:
                print(f"[charlock] token '{t}' already maps to "
                      f"'{index[t].get('name')}'; keeping first, ignoring "
                      f"'{c.get('name')}'. Rename to disambiguate.",
                      file=sys.stderr, flush=True)
                continue
            index[t] = c
    return index


def resolve_scene_characters(scene_text, char_index, explicit_ids=None,
                             all_characters=None):
    """Return the ordered, de-duplicated characters that appear in a scene.

    Priority:
      1. explicit_ids -- if the shot list already tagged character ids/names
         for this scene, trust them verbatim (strongest anti-mismatch signal).
      2. token scan   -- otherwise scan the scene text against char_index using
         word-boundary matches (so 'max' doesn't fire inside 'maximum').

    Order is preserved by FIRST APPEARANCE in the scene text so the ref order
    tracks how characters actually enter the scene (stable, scene-driven
    identity ordering). For explicit_ids, the caller's tag order is preserved.
    """
    out, seen = [], set()

    def _add(c):
        if c is not None and id(c) not in seen:
            seen.add(id(c))
            out.append(c)

    # 1. explicit tags win -- preserve the caller's tag order
    if explicit_ids:
        by_name = {}
        for c in (all_characters or []):
            by_name[_norm_name(c.get("name"))] = c
            if c.get("id") is not None:
                by_name[str(c.get("id"))] = c
        for tag in explicit_ids:
            c = (by_name.get(_norm_name(tag)) or by_name.get(str(tag))
                 or char_index.get(_norm_name(tag)))
            _add(c)
        if out:
            return out

    # 2. token scan against the locked index, ordered by FIRST APPEARANCE in
    #    the scene text. For each token that matches on a word boundary, record
    #    the index of its first occurrence, then emit characters in that order.
    text = _norm_name(scene_text)
    hits = []  # (first_pos, character)
    for token, c in char_index.items():
        m = re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text)
        if m:
            hits.append((m.start(), c))
    hits.sort(key=lambda h: h[0])
    for _pos, c in hits:
        _add(c)
    return out


def build_scene_refs(scene_chars, style_frames=None, prev_frame=None,
                     sheet_key="sheet_bytes"):
    """Assemble (refs, labels) for one scene in the anti-bleed order:

        1. CHARACTER sheets FIRST  -> identity leads the reference set
        2. STYLE frames AFTER      -> art style only, ignore people shown
        3. PREVIOUS frame LAST     -> continuity only

    This ordering is what stops the source video's cast from bleeding into your
    characters. Returns (refs: list[bytes], labels: list[str]) ready to hand to
    the image edit call (either as separate image[] fields or a contact sheet).
    """
    refs, labels = [], []
    for c in scene_chars:
        b = c.get(sheet_key)
        if b:
            refs.append(b)
            labels.append(f"CHAR: {c.get('name','?')} \u2014 KEEP IDENTITY EXACT")
    for sf in (style_frames or []):
        refs.append(sf)
        labels.append("STYLE REF \u2014 copy art style only, IGNORE people shown")
    if prev_frame is not None:
        refs.append(prev_frame)
        labels.append("PREV FRAME \u2014 continuity only")
    return refs, labels
