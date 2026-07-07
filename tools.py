"""
textquest.tools
===============
Static analysis for game files: validation (errors + warnings) and a
scene-graph map. Used by `--check` / `--map` on the CLI and by the editor.
"""

from __future__ import annotations

import json
import re

_GOTO_RE = re.compile(r'"goto ([A-Za-z0-9_]+)"')
_RANDOM_GOTO_RE = re.compile(r'"random_goto ([A-Za-z0-9_ ]+)"')
_GIVE_TAKE_RE = re.compile(r'"(?:give|take) ([A-Za-z0-9_]+)')
_EQUIP_RE = re.compile(r'"(?:equip|unequip) ([A-Za-z0-9_]+)"')
_UNLOCK_RE = re.compile(r'"unlock ([A-Za-z0-9_]+)"')
_SHOP_FX_RE = re.compile(r'"shop ([A-Za-z0-9_]+)"')
_DLG_FX_RE = re.compile(r'"dialogue ([A-Za-z0-9_]+)"')


def _effect_refs(obj) -> dict[str, set[str]]:
    """Scan any JSON fragment's effect strings for references."""
    blob = json.dumps(obj)
    random_targets = set()
    for group in _RANDOM_GOTO_RE.findall(blob):
        random_targets |= set(group.split())
    return {
        "goto": set(_GOTO_RE.findall(blob)) | random_targets,
        "items": set(_GIVE_TAKE_RE.findall(blob))
                 | set(_EQUIP_RE.findall(blob)),
        "achievements": set(_UNLOCK_RE.findall(blob)),
        "shops": set(_SHOP_FX_RE.findall(blob)),
        "dialogues": set(_DLG_FX_RE.findall(blob)),
    }


def scene_targets(game: dict, scene_id: str) -> set[str]:
    """Every scene reachable in one step from <scene_id>."""
    scene = game.get("scenes", {}).get(scene_id, {})
    targets = set()
    for ch in scene.get("choices", []):
        if ch.get("goto"):
            targets.add(ch["goto"])
    targets |= _effect_refs(scene)["goto"]
    return targets


def reachable_scenes(game: dict) -> set[str]:
    """BFS from the start scene, following choices, effects, and triggers."""
    scenes = game.get("scenes", {})
    start = game.get("meta", {}).get("start") or (next(iter(scenes), None))
    if start not in scenes:
        return set()
    # trigger gotos can fire anywhere, so they're global edges
    trigger_targets = _effect_refs(game.get("triggers", []))["goto"] | {
        t["goto"] for t in game.get("triggers", []) if t.get("goto")}
    seen, frontier = {start}, [start]
    while frontier:
        current = frontier.pop()
        for target in scene_targets(game, current) | trigger_targets:
            if target in scenes and target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


def validate_game(game: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors break the game; warnings smell."""
    errors: list[str] = []
    warnings: list[str] = []
    scenes = game.get("scenes", {})
    items = game.get("items", {})
    shops = game.get("shops", {})
    dialogues = game.get("dialogues", {})
    achievements = game.get("achievements", {})
    variables = game.get("variables", {})

    if not scenes:
        return ["game has no scenes"], []
    start = game.get("meta", {}).get("start")
    if start and start not in scenes:
        errors.append(f"meta.start -> unknown scene '{start}'")

    # -- per-scene structural checks ---------------------------------- #
    for scene_id, scene in scenes.items():
        for ch in scene.get("choices", []):
            t = ch.get("goto")
            if t and t not in scenes:
                errors.append(
                    f"scene '{scene_id}': choice -> unknown scene '{t}'")
            if ch.get("shop") and ch["shop"] not in shops:
                errors.append(f"scene '{scene_id}': choice -> "
                              f"unknown shop '{ch['shop']}'")
            if ch.get("dialogue") and ch["dialogue"] not in dialogues:
                errors.append(f"scene '{scene_id}': choice -> "
                              f"unknown dialogue '{ch['dialogue']}'")
        refs = _effect_refs(scene)
        for t in refs["goto"]:
            if t not in scenes:
                errors.append(
                    f"scene '{scene_id}': effect 'goto {t}' -> unknown scene")
        for i in refs["items"]:
            if i not in items:
                warnings.append(f"scene '{scene_id}': references item '{i}' "
                                f"not defined in items")
        for a in refs["achievements"]:
            if a not in achievements:
                errors.append(f"scene '{scene_id}': 'unlock {a}' -> "
                              f"unknown achievement")
        for s in refs["shops"]:
            if s not in shops:
                errors.append(f"scene '{scene_id}': 'shop {s}' -> unknown shop")
        for d in refs["dialogues"]:
            if d not in dialogues:
                errors.append(
                    f"scene '{scene_id}': 'dialogue {d}' -> unknown dialogue")
        if not scene.get("choices") and not scene.get("end"):
            warnings.append(f"scene '{scene_id}' has no choices and no "
                            f"'end' flag (plays as an ending)")

    # -- shops ---------------------------------------------------------- #
    for shop_id, shop in shops.items():
        for entry in shop.get("stock", []):
            if entry.get("item") not in items:
                errors.append(f"shop '{shop_id}': stock item "
                              f"'{entry.get('item')}' not defined")
        for item_id in shop.get("buys", {}):
            if item_id not in items:
                errors.append(
                    f"shop '{shop_id}': buys item '{item_id}' not defined")
        cur = shop.get("currency", "gold")
        if cur not in variables:
            warnings.append(f"shop '{shop_id}': currency '{cur}' is not an "
                            f"initial variable")

    # -- dialogues ------------------------------------------------------ #
    for dlg_id, dlg in dialogues.items():
        nodes = dlg.get("nodes", {})
        if dlg.get("start") not in nodes:
            errors.append(f"dialogue '{dlg_id}': start node "
                          f"'{dlg.get('start')}' not found")
        for node_id, node in nodes.items():
            for r in node.get("responses", []):
                t = r.get("goto")
                if t and t not in nodes:
                    errors.append(f"dialogue '{dlg_id}' node '{node_id}': "
                                  f"response -> unknown node '{t}'")

    # -- items ---------------------------------------------------------- #
    for item_id, item in items.items():
        if item.get("modifiers") and not item.get("slot"):
            warnings.append(f"item '{item_id}' has modifiers but no slot — "
                            f"it can never be equipped")

    # -- reachability ----------------------------------------------------#
    reachable = reachable_scenes(game)
    for scene_id in scenes:
        if scene_id not in reachable:
            warnings.append(f"scene '{scene_id}' is unreachable from the "
                            f"start scene")
    if not any(s.get("end") for s in scenes.values()):
        warnings.append("no scene has 'end': true — the game can only end "
                        "at a scene without choices")
    return errors, warnings


def render_map(game: dict) -> str:
    """ASCII tree of the scene graph, depth-first from the start scene."""
    scenes = game.get("scenes", {})
    start = game.get("meta", {}).get("start") or next(iter(scenes), None)
    lines: list[str] = []
    seen: set[str] = set()

    def label(scene_id: str) -> str:
        scene = scenes.get(scene_id, {})
        marks = []
        if scene.get("end"):
            marks.append("END")
        for ch in scene.get("choices", []):
            if ch.get("shop"):
                marks.append(f"shop:{ch['shop']}")
            if ch.get("dialogue"):
                marks.append(f"talk:{ch['dialogue']}")
        return scene_id + (f"  [{', '.join(marks)}]" if marks else "")

    def walk(scene_id: str, prefix: str, is_last: bool, is_root: bool):
        connector = "" if is_root else ("└─▶ " if is_last else "├─▶ ")
        if scene_id not in scenes:
            lines.append(f"{prefix}{connector}{scene_id}  [MISSING!]")
            return
        if scene_id in seen:
            lines.append(f"{prefix}{connector}{scene_id}  (…)")
            return
        seen.add(scene_id)
        lines.append(f"{prefix}{connector}{label(scene_id)}")
        children = sorted(scene_targets(game, scene_id))
        child_prefix = prefix + ("" if is_root else
                                 ("    " if is_last else "│   "))
        for i, child in enumerate(children):
            walk(child, child_prefix, i == len(children) - 1, False)

    if start:
        walk(start, "", True, True)
    trigger_targets = sorted(
        {t["goto"] for t in game.get("triggers", []) if t.get("goto")}
        | _effect_refs(game.get("triggers", []))["goto"])
    drawn_extra = [t for t in trigger_targets if t not in seen]
    for t in drawn_extra:
        lines.append(f"(trigger) ─▶ {label(t) if t in scenes else t + '  [MISSING!]'}")
        seen.add(t)
    unreachable = [s for s in scenes if s not in reachable_scenes(game)]
    if unreachable:
        lines.append("")
        lines.append("unreachable: " + ", ".join(sorted(unreachable)))
    return "\n".join(lines)
