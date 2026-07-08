"""
textquest.tools
===============
Static analysis for game files: validation (errors + warnings) and a
scene-graph map. Used by `--check` / `--map` on the CLI and by the editor.
"""

from __future__ import annotations

import json
import re

from dsl import _DICE_RE, check_expression
from ui import SYM

_GOTO_RE = re.compile(r'"goto ([A-Za-z0-9_]+)"')
_RANDOM_GOTO_RE = re.compile(r'"random_goto ([A-Za-z0-9_* ]+)"')
_GIVE_TAKE_RE = re.compile(r'"(?:give|take) ([A-Za-z0-9_]+)')
_EQUIP_RE = re.compile(r'"(?:equip|unequip) ([A-Za-z0-9_]+)"')
_UNLOCK_RE = re.compile(r'"unlock ([A-Za-z0-9_]+)"')
_SHOP_FX_RE = re.compile(r'"shop ([A-Za-z0-9_]+)"')
_DLG_FX_RE = re.compile(r'"dialogue ([A-Za-z0-9_]+)"')
_CALL_RE = re.compile(r'"call ([A-Za-z0-9_]+)"')
_DRAW_RE = re.compile(r'"draw ([A-Za-z0-9_]+)"')
_SET_RE = re.compile(r'"(?:set|roll|ask) ([A-Za-z0-9_]+) ?=')
_CONTEST_RE = re.compile(r'"contest ([A-Za-z0-9_]+) ?= ?(.+?) vs ([^"]+)"')
_TEXT_IF_RE = re.compile(r"\{if ([^{}]+?)\}")


def _effect_refs(obj) -> dict[str, set[str]]:
    """Scan any JSON fragment's effect strings for references."""
    blob = json.dumps(obj)
    random_targets = set()
    for group in _RANDOM_GOTO_RE.findall(blob):
        random_targets |= {t.partition("*")[0] for t in group.split()}
    return {
        "goto": set(_GOTO_RE.findall(blob)) | random_targets,
        "items": set(_GIVE_TAKE_RE.findall(blob))
                 | set(_EQUIP_RE.findall(blob)),
        "achievements": set(_UNLOCK_RE.findall(blob)),
        "shops": set(_SHOP_FX_RE.findall(blob)),
        "dialogues": set(_DLG_FX_RE.findall(blob)),
        "macros": set(_CALL_RE.findall(blob)),
        "tables": set(_DRAW_RE.findall(blob)),
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


def _known_variables(game: dict) -> set[str]:
    """Initial variables plus everything ever assigned by set/roll/ask/
    contest — the set a condition may legally reference."""
    blob = json.dumps(game)
    known = set(game.get("variables", {}))
    known |= set(_SET_RE.findall(blob))
    for name, _you, _them in _CONTEST_RE.findall(blob):
        known |= {name, f"{name}_you", f"{name}_them"}
    return known


def _iter_expressions(game: dict):
    """Yield (where, expression) for every condition and value expression
    in the game, so the validator can check them statically."""

    def conds(obj, where):
        """Recursively find 'if' keys and set/roll effect expressions."""
        if isinstance(obj, dict):
            if isinstance(obj.get("if"), str):
                yield where, obj["if"]
            for key, value in obj.items():
                yield from conds(value, where)
        elif isinstance(obj, list):
            for value in obj:
                yield from conds(value, where)
        elif isinstance(obj, str):
            stripped = obj.strip()
            # dice tokens (1d20) aren't Python syntax — neutralize them
            # before static checking, keeping the variable parts intact
            def dicefree(expr):
                return _DICE_RE.sub("1", expr)
            for prefix in ("set ", "roll "):
                if stripped.startswith(prefix) and "=" in stripped:
                    yield where, dicefree(stripped.split("=", 1)[1].strip())
            if stripped.startswith("contest ") and " vs " in stripped:
                body = stripped[len("contest "):]
                left, _, right = body.partition(" vs ")
                if "=" in left:
                    yield where, dicefree(left.split("=", 1)[1].strip())
                right = right.strip()
                if right not in game.get("npcs", {}):
                    yield where, dicefree(right)
            # inline {if ...} blocks inside prose and say effects
            for m in _TEXT_IF_RE.finditer(obj):
                yield where, m.group(1)

    for section in ("scenes", "shops", "dialogues", "achievements",
                    "macros", "tables", "npcs"):
        for key, value in game.get(section, {}).items():
            yield from conds(value, f"{section[:-1]} '{key}'")
    for i, trig in enumerate(game.get("triggers", [])):
        yield from conds(trig, f"trigger '{trig.get('id', i)}'")


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

    # -- macros / tables / npcs ----------------------------------------- #
    macros = game.get("macros", {})
    tables = game.get("tables", {})
    npcs = game.get("npcs", {})
    all_refs = _effect_refs(game)
    for m in all_refs["macros"]:
        if m not in macros:
            errors.append(f"effect 'call {m}' -> unknown macro")
    for t in all_refs["tables"]:
        if t not in tables:
            errors.append(f"effect 'draw {t}' -> unknown table")

    # -- expressions ------------------------------------------------------#
    known = _known_variables(game)
    seen_expr = set()
    for where, expr in _iter_expressions(game):
        if (where, expr) in seen_expr:
            continue
        seen_expr.add((where, expr))
        problem = check_expression(expr, known)
        if problem:
            errors.append(f"{where}: expression '{expr}' — {problem}")
    for npc_id, npc in npcs.items():
        spec = npc.get("roll", "")
        problem = check_expression(_DICE_RE.sub("1", spec) or "1", known)
        if problem:
            errors.append(f"npc '{npc_id}': roll '{spec}' — {problem}")

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
        connector = "" if is_root else (SYM["tree_last"] if is_last
                                        else SYM["tree_mid"])
        if scene_id not in scenes:
            lines.append(f"{prefix}{connector}{scene_id}  [MISSING!]")
            return
        if scene_id in seen:
            lines.append(f"{prefix}{connector}{scene_id}  ({SYM['more']})")
            return
        seen.add(scene_id)
        lines.append(f"{prefix}{connector}{label(scene_id)}")
        children = sorted(scene_targets(game, scene_id))
        child_prefix = prefix + ("" if is_root else
                                 ("    " if is_last else SYM["tree_pipe"]))
        for i, child in enumerate(children):
            walk(child, child_prefix, i == len(children) - 1, False)

    if start:
        walk(start, "", True, True)
    trigger_targets = sorted(
        {t["goto"] for t in game.get("triggers", []) if t.get("goto")}
        | _effect_refs(game.get("triggers", []))["goto"])
    drawn_extra = [t for t in trigger_targets if t not in seen]
    for t in drawn_extra:
        lines.append(f"(trigger) {SYM['tree_arrow']} "
                     f"{label(t) if t in scenes else t + '  [MISSING!]'}")
        seen.add(t)
    unreachable = [s for s in scenes if s not in reachable_scenes(game)]
    if unreachable:
        lines.append("")
        lines.append("unreachable: " + ", ".join(sorted(unreachable)))
    return "\n".join(lines)
