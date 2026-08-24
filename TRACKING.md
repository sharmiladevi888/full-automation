# Consistency fixes: character-lock, scene-diversity, ref feed

Tracking doc for the video-pipeline consistency work. (GitHub Issues are
disabled on this repo, so this file is the tracker.)

## Symptoms being fixed
- **Character mismatch** — a scene borrows the source video's people instead of
  the cast (scene resolves to 0 characters → falls back to style frames as the
  only human ref), or a name maps to two different sheets across scenes.
- **Duplicate / near-identical scenes** — the shot list emits the "same scene
  with different changes" instead of distinct story beats.
- **Combined-grid quality hit** — merging all refs into one contact sheet
  downsamples each ref into a small cell, softening the exact line work / palette
  we're trying to copy.

## Root causes
1. Character matching was per-scene and non-deterministic: no locked
   name/alias → sheet index, so the same token could resolve differently and
   unresolved scenes silently degraded.
2. Repetition is a **planning** problem, not a feed problem: the shot-list LLM
   generates samey scenes and didn't tag which characters are present per scene.
3. Ref delivery combined images by default (`MULTI_IMAGE_EDIT=false`).

## Status

- [x] `gen_with_refs.py`: feed refs separately (`image[]`) with contact-sheet
      fallback — commit `c10f9be`
- [x] `character_lock.py`: deterministic name→sheet locking (new module) —
      commit `9a4a7a6`
- [x] `scene_diversity.py`: near-duplicate scene guard (new module) —
      commit `319b6b4`
- [ ] Wire `character_lock` into the per-scene ref builder (see below)
- [ ] Wire `scene_diversity.assert_scene_diversity` after shot-list parse
- [ ] `claude_client.py`: harden the shot-list prompt (diff below)
- [ ] `derouter.py` `_edit_once`: bring separate-feed + grid-fallback into the
      main video pipeline (not just the standalone script)
- [ ] Per-render QA loop (character-lock pass + diversity guard) → 1-click flow

---

## Wiring 1 — character-lock (`character_lock.py`)

Build the index **once per render** (never per scene), then resolve each scene
and assemble refs in the anti-bleed order (character sheets FIRST):

```python
import character_lock

# once, before the scene loop:
char_index = character_lock.build_character_index(characters)

# per scene:
scene_chars = character_lock.resolve_scene_characters(
    scene_text=scene.get("prompt", ""),
    char_index=char_index,
    explicit_ids=scene.get("characters"),   # from the shot-list diff below
    all_characters=characters,
)
if not scene_chars:
    import sys
    print(f"[charlock] scene {i}: NO character resolved — style frames only "
          f"(identity may drift). Add explicit 'characters' tags in the shot "
          f"list.", file=sys.stderr, flush=True)

# anti-bleed ref order: CHAR sheets first, STYLE frames after, PREV frame last
refs, labels = character_lock.build_scene_refs(
    scene_chars, style_frames=style_frames, prev_frame=prev_frame,
    sheet_key="sheet_bytes",   # adjust to your character dict's image key
)
# hand refs/labels to the image edit (separate image[] or contact_sheet fallback)
```

> Adjust `sheet_key` to whatever field holds a character's sheet image bytes in
> your character dicts.

---

## Wiring 2 — scene-diversity (`scene_diversity.py`)

Call right after you parse the shot list. On duplicates, rewrite ONLY the
offending scenes instead of regenerating the whole list:

```python
import scene_diversity

scenes = extract_json(...)                     # your existing parse
dupes = scene_diversity.assert_scene_diversity(scenes, threshold=0.8)
if dupes:
    for j in scene_diversity.dedupe_indices(dupes):
        # re-ask the LLM to rewrite scenes[j] as a DIFFERENT beat
        # (new setting + action + camera), keeping the cast identical.
        ...
```

---

## Diff — `claude_client.py` shot-list prompt hardening

Find the system/user prompt that asks the model for the scene/shot list and add
these hard rules + per-scene fields. The wins: (1) per-scene `characters` tags
that feed `resolve_scene_characters` (kills mismatch), (2) an explicit
no-duplicate rule, (3) distinct camera/setting per scene.

```diff
--- a/claude_client.py
+++ b/claude_client.py
@@ shot-list / scene generation prompt
     "Return a JSON array of scenes. Each scene has: "
-    "{ \"prompt\": <image prompt> }"
+    "{ \"prompt\": <image prompt>, \"characters\": [<names present>], "
+    "\"setting\": <distinct location>, \"camera\": <distinct shot type> }\n"
+    "\n"
+    "HARD RULES (violating these = invalid output):\n"
+    "1. CHARACTERS: For every scene, list EXACTLY the characters present by "
+    "their proper names from the cast. Never invent names. If a scene has no "
+    "cast member, use an empty array. These tags are authoritative downstream.\n"
+    "2. NO DUPLICATE SCENES: No two scenes may share the SAME setting AND the "
+    "same primary action. Each scene must advance the story with a NEW beat.\n"
+    "3. DISTINCT SHOTS: Vary the `camera` across scenes (wide, close-up, "
+    "over-the-shoulder, low-angle, etc.). Do not repeat a shot type in a row.\n"
+    "4. DISTINCT SETTINGS: Prefer a fresh `setting` per scene; reusing a "
+    "location is only allowed if the action and camera both change.\n"
+    "5. CONTINUITY: Keep each character's described appearance identical across "
+    "every scene they appear in. Only pose, expression and framing may change.\n"
```

---

## The 1-click goal (real scope)

"Perfect video at 1 click" isn't a single fix — it's a QA loop: character-lock
pass + scene-diversity guard + a per-frame identity check, with automatic
targeted rewrites when any guard trips. The modules above are the building
blocks; the remaining work is wiring them into the autopilot flow and adding the
final identity-verification pass.
