"""
creator.py — the textquest authoring studio
===========================================
Create and edit games interactively, entirely in the terminal, without ever
touching JSON by hand.

Run it directly:

    python3 creator.py                  asks which file to create/edit
    python3 creator.py mygame.json      creates it if new, edits if it exists

Or through the main CLI:

    python3 textquest.py --new  mygame.json
    python3 textquest.py --edit mygame.json

Design notes: the editor mutates the same plain-dict structure the engine
consumes, so anything the editor writes, the engine can run — and
`validate_game` is shared by the editor, `--check`, and engine load time.
The killer convenience is *forward scaffolding*: point a choice at a scene
that doesn't exist yet and the editor offers to create a stub on the spot,
so you can rough out a whole story graph first and write prose later.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid

from tools import render_map, validate_game
from ui import SYM, Terminal

STUB_TEXT = "[draft] Write this scene's text."

# ---------------------------------------------------------------------- #
# Genre templates. The engine has no idea what genre a game is — variables,
# items, and currencies are whatever you name them. These just give each
# genre a sensible starting kit and a sample opening so the blank page is
# less blank. Pick "blank" for full control.
# ---------------------------------------------------------------------- #
TEMPLATES = {
    "blank": {
        "variables": {},
        "sample": "Write your opening scene here.",
    },
    "fantasy adventure": {
        "variables": {"health": 10, "gold": 5, "score": 0},
        "sample": "The tavern door creaks shut behind you. Somewhere beyond "
                  "the hills, the ruin is waiting.",
    },
    "detective mystery": {
        "variables": {"clues": 0, "suspicion": 0, "time_left": 12},
        "sample": "The call came at 3 a.m. By the time you reach the house, "
                  "the rain has washed half the evidence away.",
    },
    "science fiction": {
        "variables": {"oxygen": 100, "credits": 20, "hull": 10},
        "sample": "The station's lights flicker twice, then settle into "
                  "emergency red. You are, as far as you can tell, alone.",
    },
    "horror": {
        "variables": {"sanity": 10, "fear": 0},
        "sample": "You should not have come back to this house. You knew "
                  "that before you opened the gate.",
    },
    "slice of life / romance": {
        "variables": {"energy": 10, "affection": 0, "day": 1},
        "sample": "Monday. New town, new café job, and the espresso machine "
                  "is already hissing at you like it knows something.",
    },
    "survival": {
        "variables": {"hunger": 0, "warmth": 10, "supplies": 3, "day": 1},
        "sample": "The plane is still burning behind you. North is forest. "
                  "Everything else is snow.",
    },
}

HELP_TEXT = """
[bold underline]How the editor works[/]

A game is a set of [bold]scenes[/] (screens of text) connected by
[bold]choices[/]. The player reads a scene, picks a choice, and moves on.
Everything else — items, shops, dialogue, dice — hangs off that skeleton.

[bold]A good first session:[/]
  1. Scenes → pick your start scene → edit its text.
  2. Add choices. When a choice's 'goto' names a scene that doesn't exist,
     the editor offers to create a [yellow][draft][/] stub — say yes and keep
     going. Sketch the whole story graph this way, prose later.
  3. 'show map' to see the shape. 'validate' to catch broken links and
     drafts still to write. 'save & playtest' to try it immediately.

[bold]Effects[/] are one-line commands a choice (or scene entry, or trigger)
runs. The ones you'll use constantly:
  [cyan]set energy = energy - 1[/]        change/create any variable
  [cyan]roll check = 1d20 + skill[/]      visible dice roll into a variable
  [cyan]ask name = What's your name?[/]   store the player's typed answer
  [cyan]give keycard[/] / [cyan]take keycard[/]     inventory in/out
  [cyan]say The lights go out.[/]         print a line
  [cyan]goto scene_id[/] / [cyan]random_goto a b c[/]   jump / random jump
  [cyan]call macro[/] / [cyan]draw table[/]     reuse effect blocks / roll loot tables
  [cyan]contest x = 1d20+skill vs npc[/]  opposed roll (win if x > 0)
  [cyan]music file.ogg[/] / [cyan]sfx file.wav[/] / [cyan]pause[/] / [cyan]end[/]

[bold]Prose tricks:[/] {if has('torch')}The room is lit.{else}Darkness.{end}
inside any text; 'revisit_text' on a scene for return visits.

[bold]Workflow tools:[/] 'jump to scene', 'find text anywhere',
'save & playtest' can start from ANY scene with setup effects (test act 3
without replaying act 1), 'export prose' to write in your own editor.
Players get autosave + a 'back' (undo) command for free.

[bold]Conditions[/] gate choices, stock, dialogue lines, triggers,
achievements: [cyan]clues >= 3 and not visited('basement')[/]. You can use
any variable, plus has()/count()/equipped()/visited()/d20()/chance().

[bold]Variables are yours.[/] Nothing is built in — 'sanity', 'credits',
'affection', 'suspicion' all work the same way. Shops can trade in any
variable you name as their currency. That's how one engine covers every
genre: the mechanics don't care what the numbers mean; your story does.
"""


class Creator:
    def __init__(self, path: str, term: Terminal | None = None):
        self.path = os.path.abspath(path)
        self.term = term or Terminal()
        self.dirty = False
        if os.path.isfile(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.game = json.load(f)
            for key in ("variables", "items", "shops", "dialogues",
                        "achievements", "scenes"):
                self.game.setdefault(key, {})
            self.game.setdefault("triggers", [])
            self.game.setdefault("meta", {})
        else:
            self.game = self._wizard_new()
            self.dirty = True

    # ------------------------------------------------------------------ #
    # input helpers
    # ------------------------------------------------------------------ #
    def ask(self, label: str, default: str = "") -> str:
        hint = f" [dim]({default})[/]" if default else ""
        answer = self.term.prompt(f"[cyan]{label}{hint}:[/] ").strip()
        return answer or default

    def ask_multiline(self, label: str, current: str = "") -> str:
        self.term.echo(f"[cyan]{label}[/] [dim](finish with a single '.' on "
                       f"its own line; '.' immediately keeps current)[/]")
        if current:
            self.term.echo(f"[dim]current: {current[:200]}[/]")
        lines: list[str] = []
        while True:
            line = self.term.prompt("[dim]| [/]")
            if line.strip() == ".":
                break
            lines.append(line)
        return "\n".join(lines) if lines else current

    def ask_effects(self, label: str, current: list | None = None) -> list:
        current = current or []
        self.term.echo(f"[cyan]{label}[/] [dim](one effect per line; empty "
                       f"line = done; '.' keeps current; '?' for a "
                       f"cheatsheet)[/]")
        for eff in current:
            self.term.echo(f"[dim]current: {json.dumps(eff)}[/]", wrap=False)
        lines: list = []
        while True:
            line = self.term.prompt("[dim]fx> [/]").strip()
            if line == ".":
                return current
            if line == "?":
                self.term.echo(
                    "[dim]set v = expr · roll v = 1d20+mod · "
                    "contest v = expr vs npc · ask v = question · "
                    "give/take item · equip/unequip item · say text · "
                    "goto scene · random_goto a*3 b · call macro · "
                    "draw table · shop id · dialogue id · unlock ach · "
                    "music f.ogg · sfx f.wav · pause · end[/]\n"
                    "[dim]Start a line with 'if <condition>' for a guided "
                    "branch (then/else effects).[/]")
                continue
            if not line:
                break
            if line.startswith("if ") and len(line) > 3:
                block: dict = {"if": line[3:].strip()}
                self.term.echo("[dim]  effects when TRUE (empty line = "
                               "done):[/]")
                then = []
                while True:
                    sub = self.term.prompt("[dim]  then> [/]").strip()
                    if not sub:
                        break
                    then.append(sub)
                block["do"] = then
                if self.confirm("  Add an ELSE branch?"):
                    other = []
                    self.term.echo("[dim]  effects when FALSE:[/]")
                    while True:
                        sub = self.term.prompt("[dim]  else> [/]").strip()
                        if not sub:
                            break
                        other.append(sub)
                    block["else"] = other
                lines.append(block)
                continue
            lines.append(line)
        return lines

    def pick(self, label: str, options: list[str],
             extra: dict[str, str] | None = None):
        """Numbered picker with a fresh screen. Returns an int index into
        options, an extra-command key string, or None for back.
        (Index-based on purpose: duplicate labels must not collide.)"""
        self.term.page_break()
        self.term.echo(f"[bold bright_yellow]{self.game['meta'].get('title', '?')}"
                       f"[/] [dim]· textquest editor[/]", wrap=False)
        self.term.echo(f"\n[bold underline]{label}[/]")
        for n, opt in enumerate(options, 1):
            self.term.echo(f"  [bold cyan]{n}.[/] {opt}", wrap=False)
        for key, desc in (extra or {}).items():
            self.term.echo(f"  [bold yellow]{key}.[/] {desc}", wrap=False)
        self.term.echo("  [dim]0. back[/]", wrap=False)
        while True:
            raw = self.term.prompt("[bold magenta]> [/]").strip().lower()
            self.term.mark()
            if raw == "0" or getattr(self.term, "eof", False):
                return None
            if extra and raw in extra:
                return raw
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return int(raw) - 1
            self.term.echo("[dim]Pick a number.[/]")

    def confirm(self, label: str) -> bool:
        return self.ask(f"{label} (y/n)", "n").lower().startswith("y")

    # ------------------------------------------------------------------ #
    # scaffolding
    # ------------------------------------------------------------------ #
    def _wizard_new(self) -> dict:
        self.term.echo("[bold bright_yellow]— New game —[/]\n", wrap=False)
        title = self.ask("Game title", "Untitled")
        author = self.ask("Author", "anonymous")
        names = list(TEMPLATES)
        self.term.echo("\n[cyan]Starting template[/] [dim](just a variable "
                       "kit + sample opening — everything is editable, and "
                       "any genre works with any mechanic)[/]")
        for n, name in enumerate(names, 1):
            tvars = ", ".join(TEMPLATES[name]["variables"]) or "no variables"
            self.term.echo(f"  [bold cyan]{n}.[/] {name} [dim]({tvars})[/]",
                           wrap=False)
        choice = self.ask("Template number", "1")
        template = TEMPLATES[names[int(choice) - 1]] \
            if choice.isdigit() and 1 <= int(choice) <= len(names) \
            else TEMPLATES["blank"]
        start = self.ask("Start scene id", "intro")
        self.term.echo(f"\n[green]Scaffolded '{title}'. You're in the "
                       f"editor now — try 'scenes', or the help option.[/]")
        return {
            "meta": {"title": title, "author": author, "start": start,
                     "clear_screen": True, "typewriter_cps": 0},
            "variables": dict(template["variables"]),
            "items": {}, "shops": {}, "dialogues": {}, "achievements": {},
            "triggers": [],
            "scenes": {start: {"title": title,
                               "text": template["sample"] + "\n\n" + STUB_TEXT,
                               "choices": []}},
        }

    def _ensure_scene(self, scene_id: str) -> None:
        if scene_id in self.game["scenes"]:
            return
        if self.confirm(f"Scene '{scene_id}' doesn't exist. Create a stub?"):
            self.game["scenes"][scene_id] = {"title": scene_id,
                                             "text": STUB_TEXT, "choices": []}
            self.dirty = True

    # ------------------------------------------------------------------ #
    # scene editing
    # ------------------------------------------------------------------ #
    def edit_scenes(self) -> None:
        while True:
            scenes = self.game["scenes"]
            start = self.game["meta"].get("start")
            ids = list(scenes)
            labels = [f"{sid}{' *start' if sid == start else ''}"
                      f"{' [end]' if s.get('end') else ''}"
                      f"{' [draft]' if STUB_TEXT in s.get('text', '') else ''}"
                      f"  ({len(s.get('choices', []))} ch)"
                      for sid, s in scenes.items()]
            action = self.pick("Scenes", labels,
                               extra={"a": "add scene",
                                      "g": "jump by id / prefix"})
            if action is None:
                return
            if action == "a":
                sid = self.ask("New scene id (e.g. crime_scene, airlock)")
                if sid and sid not in scenes:
                    scenes[sid] = {"title": sid,
                                   "text": self.ask_multiline("Scene text"),
                                   "choices": []}
                    self.dirty = True
            elif action == "g":
                self.jump_to_scene()
            else:
                self.edit_scene(ids[action])

    def edit_scene(self, scene_id: str) -> None:
        scenes = self.game["scenes"]
        while True:
            scene = scenes[scene_id]
            preview = scene.get("text", "")[:150].replace("\n", " ")
            action = self.pick(
                f"Scene '{scene_id}' — {preview}",
                ["edit text", "edit title", "edit ASCII art",
                 "edit music file", "toggle 'end' flag "
                 + ("[on]" if scene.get("end") else "[off]"),
                 f"choices... ({len(scene.get('choices', []))})",
                 "on_enter effects", "on_first_enter effects",
                 "rename scene", "delete scene"])
            if action is None:
                return
            self.dirty = True
            if action == 0:
                scene["text"] = self.ask_multiline("Scene text",
                                                   scene.get("text", ""))
            elif action == 1:
                scene["title"] = self.ask("Title", scene.get("title", ""))
            elif action == 2:
                art = self.ask_multiline(
                    "ASCII art (shown above the text, verbatim)",
                    scene.get("art", ""))
                if art:
                    scene["art"] = art
                else:
                    scene.pop("art", None)
            elif action == 3:
                music = self.ask("Music file (empty to clear)",
                                 scene.get("music", ""))
                if music:
                    scene["music"] = music
                else:
                    scene.pop("music", None)
            elif action == 4:
                if scene.get("end"):
                    scene.pop("end")
                else:
                    scene["end"] = True
            elif action == 5:
                self.edit_choices(scene_id)
            elif action == 6:
                fx = self.ask_effects("on_enter", scene.get("on_enter"))
                scene["on_enter"] = fx
                if not fx:
                    scene.pop("on_enter", None)
            elif action == 7:
                fx = self.ask_effects("on_first_enter",
                                      scene.get("on_first_enter"))
                scene["on_first_enter"] = fx
                if not fx:
                    scene.pop("on_first_enter", None)
            elif action == 8:
                new_id = self.ask("New scene id", scene_id)
                if new_id and new_id != scene_id and new_id not in scenes:
                    scenes[new_id] = scenes.pop(scene_id)
                    self._retarget(scene_id, new_id)
                    scene_id = new_id
            elif action == 9:
                if self.confirm(f"Really delete '{scene_id}'?"):
                    del scenes[scene_id]
                    return

    def _retarget(self, old: str, new: str) -> None:
        for scene in self.game["scenes"].values():
            for ch in scene.get("choices", []):
                if ch.get("goto") == old:
                    ch["goto"] = new
        for trig in self.game.get("triggers", []):
            if trig.get("goto") == old:
                trig["goto"] = new
        if self.game["meta"].get("start") == old:
            self.game["meta"]["start"] = new

    def edit_choices(self, scene_id: str) -> None:
        scene = self.game["scenes"][scene_id]
        while True:
            choices = scene.setdefault("choices", [])
            labels = [f"{c.get('text', '?')[:42]}"
                      f"{'  → ' + c['goto'] if c.get('goto') else ''}"
                      f"{'  [if]' if c.get('if') else ''}"
                      f"{'  [once]' if c.get('once') else ''}"
                      for c in choices]
            action = self.pick(f"Choices of '{scene_id}'", labels,
                               extra={"a": "add choice"})
            if action is None:
                return
            self.dirty = True
            if action == "a":
                choices.append(self._choice_form({}))
                continue
            idx = action
            sub = self.pick(f"Choice: {labels[idx]}",
                            ["edit", "delete", "move up", "move down"])
            if sub == 0:
                choices[idx] = self._choice_form(choices[idx])
            elif sub == 1 and self.confirm("Delete this choice?"):
                choices.pop(idx)
            elif sub == 2 and idx > 0:
                choices[idx - 1], choices[idx] = choices[idx], choices[idx - 1]
            elif sub == 3 and idx < len(choices) - 1:
                choices[idx + 1], choices[idx] = choices[idx], choices[idx + 1]

    def _choice_form(self, ch: dict) -> dict:
        ch = dict(ch)
        ch["text"] = self.ask("Choice text", ch.get("text", ""))
        self.term.echo(f"[dim]scenes: {', '.join(sorted(self.game['scenes']))}"
                       f"[/]")
        goto = self.ask("Goto scene (empty = stay/re-render this scene)",
                        ch.get("goto", ""))
        if goto:
            self._ensure_scene(goto)
            ch["goto"] = goto
        else:
            ch.pop("goto", None)
        cond = self.ask("Condition 'if' (empty = always shown)",
                        ch.get("if", ""))
        if cond:
            ch["if"] = cond
            if self.confirm("Show dimmed when locked (show_locked)?"):
                ch["show_locked"] = True
                ch["locked_text"] = self.ask("Locked reason",
                                             ch.get("locked_text", "locked"))
            else:
                ch.pop("show_locked", None)
                ch.pop("locked_text", None)
        else:
            for k in ("if", "show_locked", "locked_text"):
                ch.pop(k, None)
        if self.confirm("One-time only (disappears after being picked)?"):
            ch["once"] = True
        else:
            ch.pop("once", None)
        fx = self.ask_effects("Effects (do)", ch.get("do"))
        if fx:
            ch["do"] = fx
        else:
            ch.pop("do", None)
        if self.game["shops"]:
            self.term.echo(f"[dim]shops: {', '.join(self.game['shops'])}[/]")
        shop = self.ask("Open shop id (empty = none)", ch.get("shop", ""))
        if shop:
            ch["shop"] = shop
        else:
            ch.pop("shop", None)
        if self.game["dialogues"]:
            self.term.echo(f"[dim]dialogues: "
                           f"{', '.join(self.game['dialogues'])}[/]")
        dlg = self.ask("Open dialogue id (empty = none)",
                       ch.get("dialogue", ""))
        if dlg:
            ch["dialogue"] = dlg
        else:
            ch.pop("dialogue", None)
        return ch

    # ------------------------------------------------------------------ #
    # items / variables
    # ------------------------------------------------------------------ #
    def edit_items(self) -> None:
        while True:
            items = self.game["items"]
            ids = list(items)
            labels = [f"{iid} — {it.get('name', '?')}"
                      f"{'  [' + it['slot'] + ']' if it.get('slot') else ''}"
                      for iid, it in items.items()]
            action = self.pick("Items", labels, extra={"a": "add item"})
            if action is None:
                return
            self.dirty = True
            if action == "a":
                iid = self.ask("Item id (e.g. keycard, love_letter, flare)")
                if not iid or iid in items:
                    continue
            else:
                iid = ids[action]
                if self.confirm("Delete this item instead of editing?"):
                    del items[iid]
                    continue
            item = items.get(iid, {})
            item["name"] = self.ask("Display name", item.get("name", iid))
            item["description"] = self.ask("Description",
                                           item.get("description", ""))
            slot = self.ask("Equipment slot (any word: hand/head/badge/…; "
                            "empty = not wearable)", item.get("slot", ""))
            if slot:
                item["slot"] = slot
                mods = dict(item.get("modifiers", {}))
                self.term.echo("[dim]Variable bonuses while equipped. Empty "
                               "name = done; 0 removes an entry.[/]")
                for var, val in mods.items():
                    self.term.echo(f"[dim]current: {var} {val:+d}[/]")
                while True:
                    var = self.ask("  modifier variable")
                    if not var:
                        break
                    try:
                        val = int(self.ask(f"  {var} bonus (e.g. 2 or -1)",
                                           "1"))
                        if val == 0:
                            mods.pop(var, None)
                        else:
                            mods[var] = val
                    except ValueError:
                        pass
                if mods:
                    item["modifiers"] = mods
                else:
                    item.pop("modifiers", None)
            else:
                item.pop("slot", None)
                item.pop("modifiers", None)
            items[iid] = item

    def edit_variables(self) -> None:
        while True:
            var = self.game["variables"]
            names = list(var)
            labels = [f"{k} = {v!r}" for k, v in var.items()]
            action = self.pick("Starting variables", labels,
                               extra={"a": "add variable"})
            if action is None:
                return
            self.dirty = True
            if action == "a":
                name = self.ask("Variable name (anything: sanity, credits, "
                                "affection, fuel…)")
                if not name:
                    continue
            else:
                name = names[action]
                if self.confirm("Delete this variable instead of editing?"):
                    del var[name]
                    continue
            raw = self.ask(f"Initial value for '{name}' (number, true/false, "
                           f"or text)", str(var.get(name, 0)))
            try:
                var[name] = json.loads(raw.lower() if raw.lower() in
                                       ("true", "false") else raw)
            except (json.JSONDecodeError, ValueError):
                var[name] = raw

    # ------------------------------------------------------------------ #
    # shops
    # ------------------------------------------------------------------ #
    def edit_shops(self) -> None:
        while True:
            shops = self.game["shops"]
            ids = list(shops)
            labels = [f"{sid} — {s.get('name', '?')} "
                      f"({len(s.get('stock', []))} stock, "
                      f"currency: {s.get('currency', 'gold')})"
                      for sid, s in shops.items()]
            action = self.pick("Shops / traders / vending machines", labels,
                               extra={"a": "add shop"})
            if action is None:
                return
            self.dirty = True
            if action == "a":
                sid = self.ask("Shop id (e.g. peddler, vending_machine, "
                               "black_market)")
                if not sid or sid in shops:
                    continue
                shops[sid] = {"name": sid,
                              "currency": self.ask(
                                  "Currency variable (any variable: gold, "
                                  "credits, favors…)", "gold"),
                              "stock": [], "buys": {}}
                self._shop_form(sid)
            else:
                self._shop_form(ids[action])

    def _shop_form(self, sid: str) -> None:
        while True:
            shop = self.game["shops"][sid]
            stock_desc = ", ".join(e.get("item", "?")
                                   for e in shop.get("stock", [])) or "(none)"
            action = self.pick(
                f"Shop '{sid}' — stock: {stock_desc}",
                ["name", "currency variable", "greeting line",
                 "add stock entry", "remove stock entry",
                 "items the shop buys from the player", "delete shop"])
            if action is None:
                return
            self.dirty = True
            if action == 0:
                shop["name"] = self.ask("Shop name", shop.get("name", sid))
            elif action == 1:
                shop["currency"] = self.ask("Currency variable",
                                            shop.get("currency", "gold"))
            elif action == 2:
                shop["greeting"] = self.ask("Greeting line",
                                            shop.get("greeting", ""))
            elif action == 3:
                self.term.echo(f"[dim]items: "
                               f"{', '.join(self.game['items']) or '(none — add items first)'}[/]")
                item = self.ask("Item id")
                if not item:
                    continue
                entry: dict = {"item": item}
                try:
                    entry["price"] = int(self.ask("Price", "1"))
                except ValueError:
                    entry["price"] = 1
                qty = self.ask("Stock quantity (empty = unlimited)")
                if qty.isdigit():
                    entry["qty"] = int(qty)
                cond = self.ask("Condition 'if' (empty = always)")
                if cond:
                    entry["if"] = cond
                fx = self.ask_effects("on_buy effects")
                if fx:
                    entry["on_buy"] = fx
                shop.setdefault("stock", []).append(entry)
            elif action == 4:
                stock = shop.get("stock", [])
                pick = self.pick("Remove which?",
                                 [f"{e.get('item')} @ {e.get('price')}"
                                  for e in stock])
                if pick is not None:
                    stock.pop(pick)
            elif action == 5:
                buys = shop.setdefault("buys", {})
                self.term.echo("[dim]Empty item id = done. Price 0 removes "
                               "an entry.[/]")
                for k, v in buys.items():
                    self.term.echo(f"[dim]current: buys {k} for {v}[/]")
                while True:
                    item = self.ask("  item id")
                    if not item:
                        break
                    try:
                        price = int(self.ask("  sell price", "1"))
                    except ValueError:
                        continue
                    if price <= 0:
                        buys.pop(item, None)
                    else:
                        buys[item] = price
            elif action == 6:
                if self.confirm(f"Really delete shop '{sid}'?"):
                    del self.game["shops"][sid]
                    return

    # ------------------------------------------------------------------ #
    # dialogues
    # ------------------------------------------------------------------ #
    def edit_dialogues(self) -> None:
        while True:
            dlgs = self.game["dialogues"]
            ids = list(dlgs)
            labels = [f"{did} — {d.get('name', '?')} "
                      f"({len(d.get('nodes', {}))} nodes)"
                      for did, d in dlgs.items()]
            action = self.pick("Dialogues (conversations with characters)",
                               labels, extra={"a": "add dialogue"})
            if action is None:
                return
            self.dirty = True
            if action == "a":
                did = self.ask("Dialogue id (e.g. suspect_interview, "
                               "ship_computer)")
                if not did or did in dlgs:
                    continue
                dlgs[did] = {"name": self.ask("Speaker name", did),
                             "start": "hello",
                             "nodes": {"hello": {"text": "\"...\"",
                                                 "responses": []}}}
                self._dialogue_form(did)
            else:
                self._dialogue_form(ids[action])

    def _dialogue_form(self, did: str) -> None:
        while True:
            dlg = self.game["dialogues"][did]
            nodes = dlg.setdefault("nodes", {})
            ids = list(nodes)
            labels = [f"{nid}{' *start' if nid == dlg.get('start') else ''}"
                      f" — {n.get('text', '')[:40]}"
                      for nid, n in nodes.items()]
            action = self.pick(
                f"Dialogue '{did}' — speaker: {dlg.get('name')}", labels,
                extra={"a": "add node", "n": "rename speaker",
                       "s": "set start node", "x": "delete dialogue"})
            if action is None:
                return
            self.dirty = True
            if action == "a":
                nid = self.ask("Node id")
                if nid and nid not in nodes:
                    nodes[nid] = {"text": "", "responses": []}
                    self._node_form(did, nid)
            elif action == "n":
                dlg["name"] = self.ask("Speaker name", dlg.get("name", did))
            elif action == "s":
                start = self.ask("Start node", dlg.get("start", ""))
                if start in nodes:
                    dlg["start"] = start
            elif action == "x":
                if self.confirm(f"Really delete dialogue '{did}'?"):
                    del self.game["dialogues"][did]
                    return
            else:
                self._node_form(did, ids[action])

    def _node_form(self, did: str, nid: str) -> None:
        dlg = self.game["dialogues"][did]
        while True:
            if nid not in dlg["nodes"]:
                return
            node = dlg["nodes"][nid]
            responses = node.setdefault("responses", [])
            labels = [f"{r.get('text', '?')[:40]}"
                      f"{'  → ' + r['goto'] if r.get('goto') else ''}"
                      f"{'  [exit]' if r.get('exit') else ''}"
                      for r in responses]
            action = self.pick(
                f"Node '{nid}' — {node.get('text', '')[:80]}", labels,
                extra={"t": "edit the speaker's line",
                       "e": "node effects (run when line is spoken)",
                       "a": "add player response", "x": "delete node"})
            if action is None:
                return
            self.dirty = True
            if action == "t":
                node["text"] = self.ask("Speaker line", node.get("text", ""))
            elif action == "e":
                fx = self.ask_effects("Node effects", node.get("do"))
                if fx:
                    node["do"] = fx
                else:
                    node.pop("do", None)
            elif action == "a":
                responses.append(self._response_form(did, {}))
            elif action == "x":
                if self.confirm(f"Delete node '{nid}'?"):
                    del dlg["nodes"][nid]
                    return
            else:
                idx = action
                sub = self.pick("Response", ["edit", "delete"])
                if sub == 0:
                    responses[idx] = self._response_form(did, responses[idx])
                elif sub == 1:
                    responses.pop(idx)

    def _response_form(self, did: str, r: dict) -> dict:
        r = dict(r)
        nodes = self.game["dialogues"][did]["nodes"]
        r["text"] = self.ask("Player line", r.get("text", ""))
        self.term.echo(f"[dim]nodes: {', '.join(nodes)}[/]")
        goto = self.ask("Goto node (empty = repeat this node)",
                        r.get("goto", ""))
        if goto:
            if goto not in nodes and self.confirm(
                    f"Node '{goto}' doesn't exist. Create it?"):
                nodes[goto] = {"text": "", "responses": []}
            r["goto"] = goto
        else:
            r.pop("goto", None)
        if self.confirm("Ends the conversation (exit)?"):
            r["exit"] = True
        else:
            r.pop("exit", None)
        cond = self.ask("Condition 'if' (empty = always)", r.get("if", ""))
        if cond:
            r["if"] = cond
        else:
            r.pop("if", None)
        fx = self.ask_effects("Effects (do)", r.get("do"))
        if fx:
            r["do"] = fx
        else:
            r.pop("do", None)
        return r

    # ------------------------------------------------------------------ #
    # achievements / triggers / meta
    # ------------------------------------------------------------------ #
    def edit_achievements(self) -> None:
        while True:
            achs = self.game["achievements"]
            ids = list(achs)
            labels = [f"{aid} — {a.get('name', '?')}"
                      f"{'  [auto]' if a.get('if') else '  [manual]'}"
                      f"{'  [secret]' if a.get('secret') else ''}"
                      for aid, a in achs.items()]
            action = self.pick("Achievements", labels,
                               extra={"a": "add achievement"})
            if action is None:
                return
            self.dirty = True
            if action == "a":
                aid = self.ask("Achievement id (e.g. case_closed)")
                if not aid or aid in achs:
                    continue
            else:
                aid = ids[action]
                if self.confirm("Delete instead of editing?"):
                    del achs[aid]
                    continue
            a = achs.get(aid, {})
            a["name"] = self.ask("Name", a.get("name", aid))
            a["description"] = self.ask("Description",
                                        a.get("description", ""))
            cond = self.ask("Auto-unlock condition 'if' (empty = manual via "
                            "'unlock' effect)", a.get("if", ""))
            if cond:
                a["if"] = cond
            else:
                a.pop("if", None)
            if self.confirm("Secret (hidden until earned)?"):
                a["secret"] = True
            else:
                a.pop("secret", None)
            reward = self.ask_effects("Reward effects", a.get("reward"))
            if reward:
                a["reward"] = reward
            else:
                a.pop("reward", None)
            achs[aid] = a

    def edit_triggers(self) -> None:
        """Global rules checked after every action, e.g. 'oxygen <= 0 ->
        suffocate scene' or 'suspicion >= 10 -> arrested scene'."""
        while True:
            trigs = self.game["triggers"]
            labels = [f"{t.get('id', f'trigger_{i}')}: if {t.get('if', '?')}"
                      f"{'  → ' + t['goto'] if t.get('goto') else ''}"
                      f"{'  [repeats]' if not t.get('once', True) else ''}"
                      for i, t in enumerate(trigs)]
            action = self.pick("Triggers (global rules)", labels,
                               extra={"a": "add trigger"})
            if action is None:
                return
            self.dirty = True
            if action == "a":
                trig: dict = {"id": self.ask("Trigger id (e.g. death_check)")}
                trigs.append(trig)
            else:
                sub = self.pick(f"Trigger: {labels[action]}",
                                ["edit", "delete"])
                if sub == 1:
                    trigs.pop(action)
                    continue
                if sub != 0:
                    continue
                trig = trigs[action]
            trig["if"] = self.ask("Condition 'if'", trig.get("if", ""))
            goto = self.ask("Goto scene (empty = none)", trig.get("goto", ""))
            if goto:
                self._ensure_scene(goto)
                trig["goto"] = goto
            else:
                trig.pop("goto", None)
            trig["once"] = not self.confirm(
                "Repeatable (fires every time the condition is true — "
                "needed for death checks)?")
            fx = self.ask_effects("Effects (do)", trig.get("do"))
            if fx:
                trig["do"] = fx
            else:
                trig.pop("do", None)

    def edit_meta(self) -> None:
        meta = self.game["meta"]
        meta["title"] = self.ask("Title", meta.get("title", ""))
        meta["author"] = self.ask("Author", meta.get("author", ""))
        start = self.ask("Start scene", meta.get("start", ""))
        if start:
            self._ensure_scene(start)
            meta["start"] = start
        try:
            meta["typewriter_cps"] = int(self.ask(
                "Typewriter speed, chars/sec (0 = off)",
                str(meta.get("typewriter_cps", 0))))
        except ValueError:
            pass
        meta["clear_screen"] = self.confirm("Clear screen between scenes?")
        self.dirty = True

    # ------------------------------------------------------------------ #
    # navigation & search — the "faster than writing code" tools
    # ------------------------------------------------------------------ #
    def jump_to_scene(self) -> None:
        scenes = self.game["scenes"]
        query = self.ask("Scene id (a unique prefix works)")
        if not query:
            return
        matches = [sid for sid in scenes if sid == query] or \
                  [sid for sid in scenes if sid.startswith(query)]
        if len(matches) == 1:
            self.edit_scene(matches[0])
        elif matches:
            self.term.echo(f"[dim]ambiguous: {', '.join(matches)}[/]")
            self.term.pause(force=True)
        else:
            if self.confirm(f"No scene matches '{query}'. Create it?"):
                scenes[query] = {"title": query, "text": STUB_TEXT,
                                 "choices": []}
                self.dirty = True
                self.edit_scene(query)

    def search_text(self) -> None:
        query = self.ask("Search all game text for").lower()
        if not query:
            return
        hits: list[tuple[str, str, str]] = []  # (kind, id, snippet)

        def snip(text: str) -> str:
            at = text.lower().find(query)
            return text[max(0, at - 30):at + 40].replace("\n", " ")

        for sid, scene in self.game["scenes"].items():
            hay = " ".join([scene.get("text", ""), scene.get("title", ""),
                            scene.get("revisit_text", "")])
            if query in hay.lower():
                hits.append(("scene", sid, snip(hay)))
            for ch in scene.get("choices", []):
                if query in ch.get("text", "").lower():
                    hits.append(("scene", sid,
                                 "choice: " + snip(ch["text"])))
        for did, dlg in self.game["dialogues"].items():
            for nid, node in dlg.get("nodes", {}).items():
                hay = node.get("text", "") + " ".join(
                    r.get("text", "") for r in node.get("responses", []))
                if query in hay.lower():
                    hits.append(("dialogue", did, f"node {nid}: {snip(hay)}"))
        if not hits:
            self.term.echo(f"[dim]No hits for '{query}'.[/]")
            self.term.pause(force=True)
            return
        labels = [f"[{kind} {ident}] {SYM['more']}{snippet}{SYM['more']}"
                  for kind, ident, snippet in hits]
        picked = self.pick(f"Hits for '{query}'", labels)
        if picked is None:
            return
        kind, ident, _ = hits[picked]
        if kind == "scene":
            self.edit_scene(ident)
        else:
            self._dialogue_form(ident)

    # ------------------------------------------------------------------ #
    # advanced content: macros, tables, npcs
    # ------------------------------------------------------------------ #
    def edit_advanced(self) -> None:
        while True:
            g = self.game
            action = self.pick(
                "Advanced (reusable building blocks)",
                [f"effect macros ({len(g.get('macros', {}))}) — shared "
                 f"effect lists, run with 'call name'",
                 f"random tables ({len(g.get('tables', {}))}) — weighted "
                 f"outcomes, run with 'draw name'",
                 f"NPC stat blocks ({len(g.get('npcs', {}))}) — reusable "
                 f"opponents for 'contest x = expr vs npc'"])
            if action is None:
                return
            (self.edit_macros, self.edit_tables, self.edit_npcs)[action]()

    def edit_macros(self) -> None:
        while True:
            macros = self.game.setdefault("macros", {})
            ids = list(macros)
            labels = [f"{mid}  ({len(fx)} effect(s))"
                      for mid, fx in macros.items()]
            action = self.pick("Effect macros", labels,
                               extra={"a": "add macro"})
            if action is None:
                return
            self.dirty = True
            if action == "a":
                mid = self.ask("Macro name (e.g. take_damage, pass_time)")
                if not mid or mid in macros:
                    continue
            else:
                mid = ids[action]
                if self.confirm("Delete this macro instead of editing?"):
                    del macros[mid]
                    continue
            macros[mid] = self.ask_effects(f"Effects of '{mid}'",
                                           macros.get(mid))

    def edit_tables(self) -> None:
        while True:
            tables = self.game.setdefault("tables", {})
            ids = list(tables)
            labels = [f"{tid}  ({len(entries)} outcome(s))"
                      for tid, entries in tables.items()]
            action = self.pick("Random tables (loot, events, encounters)",
                               labels, extra={"a": "add table"})
            if action is None:
                return
            self.dirty = True
            if action == "a":
                tid = self.ask("Table name (e.g. cave_loot, random_event)")
                if not tid or tid in tables:
                    continue
                tables[tid] = []
            else:
                tid = ids[action]
                if self.confirm("Delete this table instead of editing?"):
                    del tables[tid]
                    continue
            entries = tables[tid]
            while True:
                elabels = [f"weight {e.get('weight', 1)}: "
                           f"{'; '.join(str(x) for x in e.get('do', []))[:50]}"
                           for e in entries]
                sub = self.pick(f"Table '{tid}' — one outcome is drawn, "
                                f"odds by weight", elabels,
                                extra={"a": "add outcome"})
                if sub is None:
                    break
                if sub == "a":
                    entry: dict = {}
                    try:
                        entry["weight"] = int(self.ask("Weight", "1"))
                    except ValueError:
                        entry["weight"] = 1
                    entry["do"] = self.ask_effects("Effects of this outcome")
                    cond = self.ask("Condition 'if' (empty = always in "
                                    "the pool)")
                    if cond:
                        entry["if"] = cond
                    entries.append(entry)
                elif self.confirm("Delete this outcome?"):
                    entries.pop(sub)

    def edit_npcs(self) -> None:
        while True:
            npcs = self.game.setdefault("npcs", {})
            ids = list(npcs)
            labels = [f"{nid} — {n.get('name', '?')}  rolls "
                      f"{n.get('roll', '1d20')}" for nid, n in npcs.items()]
            action = self.pick("NPC stat blocks", labels,
                               extra={"a": "add NPC"})
            if action is None:
                return
            self.dirty = True
            if action == "a":
                nid = self.ask("NPC id (e.g. troll, guard_captain)")
                if not nid or nid in npcs:
                    continue
            else:
                nid = ids[action]
                if self.confirm("Delete this NPC instead of editing?"):
                    del npcs[nid]
                    continue
            npc = npcs.get(nid, {})
            npc["name"] = self.ask("Display name", npc.get("name", nid))
            npc["roll"] = self.ask("Contest roll (e.g. 1d20 + 4)",
                                   npc.get("roll", "1d20"))
            npcs[nid] = npc
            self.term.echo(f"[dim]Use it: contest fight = 1d20 + skill vs "
                           f"{nid}   (then branch on fight > 0)[/]")
            self.term.pause(force=True)

    # ------------------------------------------------------------------ #
    # prose export / import — write in your own editor
    # ------------------------------------------------------------------ #
    PROSE_MARK = "=== scene: "

    def export_prose(self) -> None:
        out = os.path.splitext(self.path)[0] + "_prose.txt"
        with open(out, "w", encoding="utf-8") as f:
            f.write("# Edit the text under each marker, keep the marker "
                    "lines intact,\n# then use 'import prose' in the "
                    "editor.\n\n")
            for sid, scene in self.game["scenes"].items():
                f.write(f"{self.PROSE_MARK}{sid} ===\n")
                f.write(scene.get("text", "") + "\n\n")
        self.term.echo(f"[green]Wrote {out}[/]", wrap=False)
        self.term.pause(force=True)

    def import_prose(self) -> None:
        src = os.path.splitext(self.path)[0] + "_prose.txt"
        if not os.path.isfile(src):
            self.term.echo(f"[red]{src} not found — export first.[/]")
            self.term.pause(force=True)
            return
        with open(src, encoding="utf-8") as f:
            content = f.read()
        current, buf, updated = None, [], 0

        def flush():
            nonlocal updated
            if current and current in self.game["scenes"]:
                text = "\n".join(buf).strip()
                if text and text != self.game["scenes"][current].get("text"):
                    self.game["scenes"][current]["text"] = text
                    updated += 1

        for line in content.splitlines():
            if line.startswith(self.PROSE_MARK):
                flush()
                current = line[len(self.PROSE_MARK):].rstrip("= ").strip()
                buf = []
            elif not line.startswith("#") or buf:
                buf.append(line)
        flush()
        self.dirty = updated > 0
        self.term.echo(f"[green]Updated {updated} scene(s) from {src}[/]",
                       wrap=False)
        self.term.pause(force=True)

    # ------------------------------------------------------------------ #
    # validate / save / play
    # ------------------------------------------------------------------ #
    def show_validation(self) -> None:
        errors, warnings = validate_game(self.game)
        if not errors and not warnings:
            self.term.echo(f"[bold green]{SYM['check']} No problems found.[/]")
        for e in errors:
            self.term.echo(f"[bold red]{SYM['cross']} {e}[/]")
        for w in warnings:
            self.term.echo(f"[yellow]{SYM['warn']} {w}[/]")
        drafts = [sid for sid, s in self.game["scenes"].items()
                  if STUB_TEXT in s.get("text", "")]
        if drafts:
            self.term.echo(f"[dim]drafts to write: {', '.join(drafts)}[/]")
        self.term.pause(force=True)

    def _assign_stable_ids(self) -> None:
        """One-time choices and triggers need identity that survives
        reordering, or saves silently corrupt. Assigned invisibly on save."""
        for scene in self.game["scenes"].values():
            for ch in scene.get("choices", []):
                if ch.get("once") and not ch.get("id"):
                    ch["id"] = "ch_" + uuid.uuid4().hex[:8]
        for trig in self.game.get("triggers", []):
            if not trig.get("id"):
                trig["id"] = "trig_" + uuid.uuid4().hex[:8]

    def save(self) -> None:
        self._assign_stable_ids()
        if os.path.isfile(self.path):          # never lose the last version
            try:
                shutil.copy2(self.path, self.path + ".bak")
            except OSError:
                pass
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.game, f, indent=2, ensure_ascii=False)
        self.dirty = False
        self.term.echo(f"[green]Saved {self.path}[/] "
                       f"[dim](previous version in .bak)[/]", wrap=False)

    def playtest(self) -> None:
        self.save()
        from engine import Engine
        scenes = self.game["scenes"]
        start = self.ask("Start from scene (empty = normal start)")
        if start and start not in scenes:
            matches = [sid for sid in scenes if sid.startswith(start)]
            start = matches[0] if len(matches) == 1 else ""
        setup = []
        if start:
            self.term.echo("[dim]Optional setup run before playing — e.g. "
                           "'set gold = 50', 'give torch', 'equip charm'. "
                           "Lets you test act 3 without replaying act 1.[/]")
            setup = self.ask_effects("Setup effects")
        self.term.echo("[dim]— playtest (debug on) —[/]", wrap=False)
        try:
            Engine(self.path, debug=True, start_scene=start or None,
                   setup_effects=setup).run()
        except Exception as e:  # never let a playtest crash the editor
            self.term.echo(f"[bold red]Playtest crashed: {e}[/]")
        self.term.pause(force=True)

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        while True:
            g = self.game
            summary = (f"{len(g['scenes'])} scenes · {len(g['items'])} items "
                       f"· {len(g['variables'])} vars · "
                       f"{len(g['shops'])} shops · "
                       f"{len(g['dialogues'])} dialogues"
                       + (" · [yellow]UNSAVED[/]" if self.dirty else ""))
            actions = [
                ("scenes...", self.edit_scenes),
                ("jump to scene (by id)", self.jump_to_scene),
                ("find text anywhere", self.search_text),
                ("items...", self.edit_items),
                ("starting variables...", self.edit_variables),
                ("shops...", self.edit_shops),
                ("dialogues...", self.edit_dialogues),
                ("achievements...", self.edit_achievements),
                ("triggers (global rules)...", self.edit_triggers),
                ("advanced: macros, tables, NPCs...", self.edit_advanced),
                ("game settings (meta)", self.edit_meta),
                ("validate", self.show_validation),
                ("show map", None),
                ("save", None),
                ("save & playtest", self.playtest),
                ("export prose to .txt (write in your own editor)",
                 self.export_prose),
                ("import prose back from .txt", self.import_prose),
                ("help — how this all works", None),
            ]
            action = self.pick(f"Main menu   [dim]{summary}[/]",
                               [label for label, _ in actions])
            if action is None:
                if self.dirty and not self.confirm(
                        "Quit without saving changes?"):
                    continue
                return
            label, handler = actions[action]
            if label == "show map":
                self.term.echo(render_map(self.game), wrap=False)
                self.term.pause(force=True)
            elif label == "save":
                self.save()
                self.term.pause(force=True)
            elif label.startswith("help"):
                self.term.echo(HELP_TEXT)
                self.term.pause(force=True)
            else:
                handler()


# ---------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="creator",
        description="textquest game editor — creates the file if it doesn't "
                    "exist, edits it if it does.")
    parser.add_argument("game", nargs="?", default=None,
                        help="path to the game .json file")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)

    term = Terminal(use_color=False if args.no_color else None)
    path = args.game
    if not path:
        term.echo("[bold bright_yellow]textquest editor[/]", wrap=False)
        path = term.prompt(
            "Game file to create or edit (e.g. mygame.json): ").strip()
        if not path:
            term.echo("No file given. Bye.")
            return 1
        if not path.endswith(".json"):
            path += ".json"
    Creator(path, term=term).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
