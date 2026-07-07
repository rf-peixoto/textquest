"""
textquest.engine
================
The core: loads a game definition (JSON) and runs the play loop.

Games are pure data. A minimal game file:

{
  "meta":   {"title": "My Game", "start": "intro"},
  "variables": {"score": 0},
  "scenes": {
    "intro": {
      "text": "You wake up in a [cyan]strange room[/].",
      "choices": [
        {"text": "Open the door", "goto": "hall"},
        {"text": "Go back to sleep", "goto": "sleep_end"}
      ]
    },
    ...
  }
}

Effects are simple command strings, executed in order:
    "set score = score + 10"     mutate/create any variable
    "give torch"  /  "give coin 5"
    "take torch"  /  "take coin 2"
    "say You hear a [red]scream[/]!"
    "sfx door.wav"
    "music dungeon.ogg"  /  "music stop"
    "goto cellar"
    "end"

Conditions ("if") are safe expressions:
    "score >= 10 and has('torch') and not visited('cellar')"
"""

from __future__ import annotations

import json
import os
import random
import shlex
import string
import sys

from audio import AudioManager
from dsl import Evaluator, ExpressionError, _DICE_RE, roll_dice
from state import GameState
from ui import Terminal


class GameError(Exception):
    pass


class _TemplateFormatter(string.Formatter):
    """Formats {var} placeholders from game variables; unknown -> {var} kept."""

    def __init__(self, variables: dict):
        super().__init__()
        self.variables = variables

    def get_value(self, key, args, kwargs):
        if isinstance(key, str) and key in self.variables:
            return self.variables[key]
        return "{" + str(key) + "}"


class Engine:
    SYSTEM_COMMANDS = ("inv", "i", "save", "load", "help", "quit", "stats")

    def __init__(self, game_path: str, terminal: Terminal | None = None,
                 audio: AudioManager | None = None, save_dir: str | None = None,
                 debug: bool = False, seed: int | None = None):
        self.game_path = os.path.abspath(game_path)
        self.game_dir = os.path.dirname(self.game_path)
        self.debug = debug
        if seed is not None:
            random.seed(seed)

        with open(self.game_path, encoding="utf-8") as f:
            self.game: dict = json.load(f)

        self.meta: dict = self.game.get("meta", {})
        self.scenes: dict = self.game.get("scenes", {})
        self.items: dict = self.game.get("items", {})
        self.triggers: list = self.game.get("triggers", [])
        self.shops: dict = self.game.get("shops", {})
        self.achievements: dict = self.game.get("achievements", {})
        self.dialogues: dict = self.game.get("dialogues", {})
        if not self.scenes:
            raise GameError("Game has no scenes.")
        self.start_scene = self.meta.get("start") or next(iter(self.scenes))

        self.term = terminal or Terminal(
            typewriter_cps=float(self.meta.get("typewriter_cps", 0)))
        # assets may sit beside the game file or one level up (project root)
        asset_name = self.meta.get("asset_dir", "assets")
        candidates = [os.path.join(self.game_dir, asset_name),
                      os.path.join(os.path.dirname(self.game_dir), asset_name)]
        asset_dir = next((c for c in candidates if os.path.isdir(c)),
                         candidates[0])
        self.audio = audio or AudioManager(asset_dir=asset_dir)
        self.save_dir = save_dir or os.path.join(self.game_dir, "saves")

        self.state = GameState(self.game.get("variables", {}), self.start_scene)
        self._running = False
        self._pending_goto: str | None = None
        self._validate()

    # ------------------------------------------------------------------ #
    # validation & debugging aids
    # ------------------------------------------------------------------ #
    def _validate(self) -> None:
        """Catch broken 'goto' targets at load time instead of mid-game."""
        problems = []
        for scene_id, scene in self.scenes.items():
            for ch in scene.get("choices", []):
                target = ch.get("goto")
                if target and target not in self.scenes:
                    problems.append(
                        f"scene '{scene_id}': choice -> unknown scene '{target}'")
                shop = ch.get("shop")
                if shop and shop not in self.shops:
                    problems.append(
                        f"scene '{scene_id}': choice -> unknown shop '{shop}'")
                dlg = ch.get("dialogue")
                if dlg and dlg not in self.dialogues:
                    problems.append(
                        f"scene '{scene_id}': choice -> unknown dialogue '{dlg}'")
        for dlg_id, dlg in self.dialogues.items():
            nodes = dlg.get("nodes", {})
            start = dlg.get("start")
            if start not in nodes:
                problems.append(
                    f"dialogue '{dlg_id}': start node '{start}' not found")
            for node_id, node in nodes.items():
                for r in node.get("responses", []):
                    t = r.get("goto")
                    if t and t not in nodes:
                        problems.append(f"dialogue '{dlg_id}' node '{node_id}':"
                                        f" response -> unknown node '{t}'")
        for trig in self.triggers:
            target = trig.get("goto")
            if target and target not in self.scenes:
                problems.append(f"trigger -> unknown scene '{target}'")
        if problems:
            raise GameError("Invalid game file:\n  " + "\n  ".join(problems))

    # ------------------------------------------------------------------ #
    # expression evaluation
    # ------------------------------------------------------------------ #
    def _evaluator(self) -> Evaluator:
        st = self.state
        helpers = {
            "has": st.has,
            "count": st.count,
            "visited": st.was_visited,
            "visits": st.visit_count,
            "turn": lambda: st.turn,
            "equipped": lambda item_id: item_id in st.equipped.values(),
        }
        return Evaluator(st.variables, helpers)

    def check(self, condition: str | None) -> bool:
        if not condition:
            return True
        try:
            return bool(self._evaluator().eval(condition))
        except ExpressionError as e:
            self._warn(str(e))
            return False

    # ------------------------------------------------------------------ #
    # effects
    # ------------------------------------------------------------------ #
    def run_effects(self, effects) -> None:
        if not effects:
            return
        if isinstance(effects, str):
            effects = [effects]
        for eff in effects:
            # conditional effect block: {"if": "...", "do": [...], "else": [...]}
            if isinstance(eff, dict):
                branch = "do" if self.check(eff.get("if")) else "else"
                self.run_effects(eff.get(branch, []))
                continue
            self._run_effect_string(eff)

    def _run_effect_string(self, eff: str) -> None:
        eff = eff.strip()
        if not eff:
            return
        cmd, _, rest = eff.partition(" ")
        cmd = cmd.lower()
        rest = rest.strip()

        if cmd == "set":
            name, _, expr = rest.partition("=")
            name = name.strip()
            if not name or not expr.strip():
                self._warn(f"Bad set effect: {eff!r}")
                return
            try:
                self.state.variables[name] = self._evaluator().eval(expr.strip())
            except ExpressionError as e:
                self._warn(str(e))
        elif cmd == "give":
            parts = shlex.split(rest)
            qty = int(parts[1]) if len(parts) > 1 else 1
            self.state.give(parts[0], qty)
            name = self._item_name(parts[0])
            self.term.echo(f"[green]▸ You got: {name}"
                           f"{f' x{qty}' if qty > 1 else ''}[/]")
        elif cmd == "take":
            parts = shlex.split(rest)
            qty = int(parts[1]) if len(parts) > 1 else 1
            if parts[0] in self.state.equipped.values() \
                    and self.state.count(parts[0]) <= qty:
                self._unequip_item(parts[0], silent=True)
            if self.state.take(parts[0], qty):
                name = self._item_name(parts[0])
                self.term.echo(f"[yellow]▸ You lost: {name}"
                               f"{f' x{qty}' if qty > 1 else ''}[/]")
        elif cmd == "roll":
            # "roll attack = 1d20 + strength"  -> rolls dice, shows the roll,
            # stores the final value in the variable for later comparison.
            name, _, expr = rest.partition("=")
            name, expr = name.strip(), expr.strip()
            if not name or not expr:
                self._warn(f"Bad roll effect: {eff!r} "
                           "(expected 'roll var = 1d20 + mod')")
                return
            self._do_roll(name, expr)
        elif cmd == "shop":
            self._run_shop(rest)
        elif cmd == "dialogue":
            self._run_dialogue(rest)
        elif cmd == "equip":
            self._equip(rest)
        elif cmd == "unequip":
            self._unequip_item(rest)
        elif cmd == "unlock":
            self._unlock_achievement(rest)
        elif cmd == "say":
            self.term.echo(self._template(rest))
        elif cmd == "sfx":
            parts = shlex.split(rest)
            vol = float(parts[1]) if len(parts) > 1 else 1.0
            self.audio.play_sfx(parts[0], volume=vol)
        elif cmd == "music":
            if rest.lower() == "stop":
                self.audio.stop_music()
            else:
                parts = shlex.split(rest)
                vol = float(parts[1]) if len(parts) > 1 else 0.7
                self.audio.play_music(parts[0], volume=vol)
        elif cmd == "ask":
            # "ask hero_name = What's your name?" -> prompt the player and
            # store the answer (numbers become ints so math keeps working)
            name, _, question = rest.partition("=")
            name = name.strip()
            question = self._template(question.strip()) or f"{name}?"
            answer = self.term.prompt(f"[bold cyan]{question}[/] ").strip()
            try:
                self.state.variables[name] = int(answer)
            except ValueError:
                self.state.variables[name] = answer
        elif cmd == "random_goto":
            targets = rest.split()
            if targets:
                self._pending_goto = random.choice(targets)
        elif cmd == "pause":
            self.term.pause(force=True)
        elif cmd == "goto":
            self._pending_goto = rest
        elif cmd == "end":
            self._running = False
        else:
            self._warn(f"Unknown effect {eff!r}")

    # ------------------------------------------------------------------ #
    # dice
    # ------------------------------------------------------------------ #
    def _do_roll(self, name: str, expr: str) -> None:
        """Roll dice in <expr> (may mix variables: '1d20 + luck'), display the
        individual faces, store the total in variable <name>."""
        display_parts: list[str] = []
        eval_parts: list[str] = []
        pos = 0
        for m in _DICE_RE.finditer(expr):
            display_parts.append(expr[pos:m.start()])
            eval_parts.append(expr[pos:m.start()])
            pos = m.end()
            n = int(m.group(1) or 1)
            sides = int(m.group(2))
            faces = roll_dice(n, sides)
            shown = ",".join(str(f) for f in faces)
            display_parts.append(
                f"{n if n > 1 else ''}d{sides}"
                f"[bold bright_white][{shown}][/]")
            eval_parts.append(str(sum(faces)))
        display_parts.append(expr[pos:])
        eval_parts.append(expr[pos:])
        try:
            value = self._evaluator().eval("".join(eval_parts))
        except ExpressionError as e:
            self._warn(str(e))
            return
        self.state.variables[name] = value
        self.term.echo(f"[bold magenta]🎲[/] [cyan]{name}[/]: "
                       f"{''.join(display_parts).strip()} "
                       f"[bold]= {value}[/]", wrap=False)

    # ------------------------------------------------------------------ #
    # equipment
    # ------------------------------------------------------------------ #
    def _resolve_item_id(self, ref: str) -> str | None:
        """Match an item by id or (case-insensitive) display name."""
        if ref in self.items:
            return ref
        low = ref.lower()
        for item_id, item in self.items.items():
            if item.get("name", "").lower() == low:
                return item_id
        return None

    def _apply_modifiers(self, item_id: str, sign: int) -> None:
        for var, delta in self.items.get(item_id, {}).get("modifiers",
                                                          {}).items():
            self.state.variables[var] = \
                self.state.variables.get(var, 0) + sign * delta

    def _equip(self, ref: str) -> None:
        item_id = self._resolve_item_id(ref.strip())
        if item_id is None or not self.state.has(item_id):
            self.term.echo(f"[dim]You don't have '{ref}'.[/]")
            return
        item = self.items.get(item_id, {})
        slot = item.get("slot")
        if not slot:
            self.term.echo(f"[dim]{item.get('name', item_id)} "
                           f"can't be equipped.[/]")
            return
        current = self.state.equipped.get(slot)
        if current == item_id:
            self.term.echo(f"[dim]{item.get('name', item_id)} "
                           f"is already equipped.[/]")
            return
        if current:
            self._unequip_item(current)
        self.state.equipped[slot] = item_id
        self._apply_modifiers(item_id, +1)
        mods = item.get("modifiers", {})
        mod_str = ", ".join(f"{'+' if v >= 0 else ''}{v} {k}"
                            for k, v in mods.items())
        self.term.echo(f"[green]▸ Equipped {item.get('name', item_id)} "
                       f"({slot})" + (f" — {mod_str}" if mod_str else "")
                       + "[/]")
        self._check_achievements()

    def _unequip_item(self, ref: str, silent: bool = False) -> None:
        item_id = self._resolve_item_id(ref.strip()) or ref.strip()
        for slot, equipped_id in list(self.state.equipped.items()):
            if equipped_id == item_id:
                del self.state.equipped[slot]
                self._apply_modifiers(item_id, -1)
                if not silent:
                    self.term.echo(f"[yellow]▸ Unequipped "
                                   f"{self._item_name(item_id)}.[/]")
                return
        if not silent:
            self.term.echo(f"[dim]'{ref}' isn't equipped.[/]")

    # ------------------------------------------------------------------ #
    # dialogue trees
    # ------------------------------------------------------------------ #
    def _run_dialogue(self, dlg_id: str) -> None:
        dlg = self.dialogues.get(dlg_id)
        if dlg is None:
            self._warn(f"Unknown dialogue {dlg_id!r}")
            return
        nodes = dlg.get("nodes", {})
        speaker = dlg.get("name", dlg_id)
        node_id = dlg.get("start")
        self.term.echo()
        while node_id:
            node = nodes.get(node_id)
            if node is None:
                self._warn(f"Unknown dialogue node {node_id!r}")
                return
            self.term.echo(f"[bold bright_cyan]{speaker}:[/] "
                           f"{self._template(node.get('text', '...'))}")
            self.run_effects(node.get("do", []))
            self._check_achievements()

            responses = [r for r in node.get("responses", [])
                         if self.check(r.get("if"))]
            if not responses:
                break  # NPC gets the last word; conversation ends

            self.term.echo()
            for n, r in enumerate(responses, 1):
                self.term.echo(f"  [bold cyan]{n}.[/] "
                               f"{self._template(r.get('text', '...'))}",
                               wrap=False)
            while True:
                raw = self.term.prompt("[bold magenta]you> [/]").strip()
                if raw.isdigit() and 1 <= int(raw) <= len(responses):
                    picked = responses[int(raw) - 1]
                    break
                if getattr(self.term, "eof", False):
                    return
                self.term.echo("[dim]Type a number.[/]")

            you = self._template(picked.get("text", ""))
            self.term.echo(f"[dim]You: {you}[/]")
            self.run_effects(picked.get("do", []))
            self._check_achievements()
            if picked.get("exit"):
                break
            node_id = picked.get("goto", node_id)  # no goto -> repeat node
        self.term.echo()


    def _run_shop(self, shop_id: str) -> None:
        shop = self.shops.get(shop_id)
        if shop is None:
            self._warn(f"Unknown shop {shop_id!r}")
            return
        currency = shop.get("currency", "gold")
        stock_state = self.state.shop_stock.setdefault(shop_id, {})

        while True:
            if self.meta.get("clear_screen", True):
                self.term.page_break()  # pause on unread feedback, then wipe
            wallet = self.state.variables.get(currency, 0)
            self.term.echo()
            self.term.rule("═")
            self.term.echo(f"[bold bright_yellow]{shop.get('name', shop_id)}[/]"
                           f"   [dim]|[/]   you have [yellow]{wallet} "
                           f"{currency}[/]", wrap=False)
            if shop.get("greeting"):
                self.term.echo(f"[italic]{self._template(shop['greeting'])}[/]")
            self.term.rule("═")

            entries: list[tuple] = []
            for idx, s in enumerate(shop.get("stock", [])):
                if not self.check(s.get("if")):
                    continue
                left = stock_state.get(str(idx), s.get("qty"))
                if left == 0:
                    continue
                entries.append(("buy", s, left, idx))
            for item_id, price in shop.get("buys", {}).items():
                have = self.state.count(item_id)
                if have > 0:
                    entries.append(("sell", {"item": item_id, "price": price},
                                    have, None))

            if not entries:
                self.term.echo("[dim]  Nothing to trade.[/]")
            n = 0
            for kind, entry, amount, _idx in entries:
                n += 1
                name = self._item_name(entry["item"])
                price = entry["price"]
                if kind == "buy":
                    left = f" [dim]({amount} left)[/]" if amount else ""
                    self.term.echo(f"  [bold cyan]{n}.[/] Buy [bold]{name}[/] "
                                   f"— [yellow]{price} {currency}[/]{left}",
                                   wrap=False)
                else:
                    self.term.echo(f"  [bold cyan]{n}.[/] Sell [bold]{name}[/] "
                                   f"— [green]+{entry['price']} {currency}[/] "
                                   f"[dim](you have {amount})[/]", wrap=False)
            self.term.echo(f"  [dim]{n + 1}. Leave[/]", wrap=False)

            raw = self.term.prompt("[bold magenta]shop> [/]").strip().lower()
            if raw in ("leave", "exit", "q", "quit", str(n + 1)):
                break
            if not raw.isdigit() or not (1 <= int(raw) <= len(entries)):
                self.term.echo("[dim]Type a number, or 'leave'.[/]")
                continue

            kind, entry, amount, idx = entries[int(raw) - 1]
            item_id, price = entry["item"], entry["price"]
            if kind == "buy":
                if wallet < price:
                    self.term.echo(f"[red]Not enough {currency}.[/]")
                    continue
                self.state.variables[currency] = wallet - price
                self.state.give(item_id)
                if entry.get("qty") is not None:
                    stock_state[str(idx)] = (
                        stock_state.get(str(idx), entry["qty"]) - 1)
                self.term.echo(f"[green]▸ Bought {self._item_name(item_id)} "
                               f"for {price} {currency}.[/]")
                self.run_effects(entry.get("on_buy", []))
            else:
                if not self.state.take(item_id):
                    continue
                self.state.variables[currency] = wallet + price
                self.term.echo(f"[green]▸ Sold {self._item_name(item_id)} "
                               f"for {price} {currency}.[/]")
                self.run_effects(entry.get("on_sell", []))
            self._check_achievements()

    # ------------------------------------------------------------------ #
    # achievements
    # ------------------------------------------------------------------ #
    def _unlock_achievement(self, ach_id: str) -> None:
        if ach_id in self.state.achievements:
            return
        ach = self.achievements.get(ach_id)
        if ach is None:
            self._warn(f"Unknown achievement {ach_id!r}")
            return
        self.state.achievements.append(ach_id)
        self.term.echo()
        self.term.echo(f"[bold on_yellow][black] 🏆 ACHIEVEMENT UNLOCKED "
                       f"[/][/] [bold bright_yellow]{ach.get('name', ach_id)}"
                       f"[/]", wrap=False)
        if ach.get("description"):
            self.term.echo(f"   [dim]{ach['description']}[/]", wrap=False)
        self.run_effects(ach.get("reward", []))

    def _check_achievements(self) -> None:
        """Auto-unlock any achievement whose 'if' condition is now true.
        Achievements without an 'if' are manual-only (via the unlock effect)."""
        for ach_id, ach in self.achievements.items():
            if ach_id in self.state.achievements:
                continue
            cond = ach.get("if")
            if cond and self.check(cond):
                self._unlock_achievement(ach_id)

    def _show_achievements(self) -> None:
        self.term.echo("\n[bold underline]Achievements[/]")
        if not self.achievements:
            self.term.echo("[dim]  This game has no achievements.[/]")
            return
        for ach_id, ach in self.achievements.items():
            name = ach.get("name", ach_id)
            desc = ach.get("description", "")
            if ach_id in self.state.achievements:
                self.term.echo(f"  [bright_yellow]🏆 {name}[/]"
                               + (f" [dim]— {desc}[/]" if desc else ""))
            elif ach.get("secret"):
                self.term.echo("  [dim]🔒 ??? — a secret achievement[/]")
            else:
                self.term.echo(f"  [dim]🔒 {name}"
                               + (f" — {desc}" if desc else "") + "[/]")

    # ------------------------------------------------------------------ #
    # rendering helpers
    # ------------------------------------------------------------------ #
    def _template(self, text: str) -> str:
        try:
            return _TemplateFormatter(self.state.variables).vformat(
                text, (), {})
        except (ValueError, KeyError, IndexError):
            return text  # stray braces in prose shouldn't crash the game

    def _item_name(self, item_id: str) -> str:
        return self.items.get(item_id, {}).get("name", item_id)

    def _warn(self, msg: str) -> None:
        if self.debug:
            self.term.echo(f"[bold red][engine] {msg}[/]", wrap=False)

    # ------------------------------------------------------------------ #
    # system commands (available at every prompt)
    # ------------------------------------------------------------------ #
    def _show_inventory(self) -> None:
        self.term.echo("\n[bold underline]Inventory[/]")
        if not self.state.inventory:
            self.term.echo("[dim]  (empty)[/]")
            return
        for item_id, qty in sorted(self.state.inventory.items()):
            item = self.items.get(item_id, {})
            name = item.get("name", item_id)
            desc = item.get("description", "")
            qty_str = f" [dim]x{qty}[/]" if qty > 1 else ""
            eq = " [bright_green][equipped][/]" \
                if item_id in self.state.equipped.values() else ""
            self.term.echo(f"  [cyan]•[/] [bold]{name}[/]{qty_str}{eq}"
                           + (f" [dim]— {desc}[/]" if desc else ""))

    def _show_stats(self) -> None:
        self.term.echo("\n[bold underline]Stats[/]")
        shown = self.meta.get("stats")  # optional whitelist of variables
        for k, v in self.state.variables.items():
            if shown and k not in shown:
                continue
            self.term.echo(f"  [cyan]{k}[/]: {v}")
        self.term.echo(f"  [dim]turn: {self.state.turn}[/]")

    def _save_path(self, slot: str) -> str:
        safe = "".join(c for c in slot if c.isalnum() or c in "-_") or "1"
        return os.path.join(self.save_dir, f"save_{safe}.json")

    def _do_save(self, slot: str = "1") -> None:
        path = self._save_path(slot)
        self.state.save(path)
        self.term.echo(f"[green]Game saved to slot '{slot}'.[/]")

    def _do_load(self, slot: str = "1") -> bool:
        path = self._save_path(slot)
        if not os.path.isfile(path):
            self.term.echo(f"[red]No save found in slot '{slot}'.[/]")
            return False
        self.state = GameState.load(path)
        self.term.echo(f"[green]Game loaded from slot '{slot}'.[/]")
        return True

    def _show_help(self) -> None:
        self.term.echo(
            "\n[bold underline]Commands[/]\n"
            "  [cyan]1..9[/]      choose an option\n"
            "  [cyan]inv[/] / [cyan]i[/]   show inventory\n"
            "  [cyan]stats[/]     show your variables\n"
            "  [cyan]ach[/]       show achievements\n"
            "  [cyan]equip[/] [dim]<item>[/] / [cyan]unequip[/] [dim]<item>[/]  manage gear\n"
            "  [cyan]save[/] [dim][slot][/]  save game (e.g. 'save 2')\n"
            "  [cyan]load[/] [dim][slot][/]  load game\n"
            "  [cyan]help[/]      this text\n"
            "  [cyan]quit[/]      exit the game")

    # ------------------------------------------------------------------ #
    # triggers
    # ------------------------------------------------------------------ #
    def _run_triggers(self) -> None:
        for i, trig in enumerate(self.triggers):
            trig_id = trig.get("id", f"__trigger_{i}")
            if trig.get("once", True) and trig_id in self.state.fired_triggers:
                continue
            if not self.check(trig.get("if")):
                continue
            if trig.get("once", True):
                self.state.fired_triggers.append(trig_id)
            self.run_effects(trig.get("do", []))
            if trig.get("goto") and trig["goto"] != self.state.current_scene:
                self._pending_goto = trig["goto"]

    # ------------------------------------------------------------------ #
    # scene handling
    # ------------------------------------------------------------------ #
    def _available_choices(self, scene_id: str, scene: dict) -> list[dict]:
        result = []
        for i, ch in enumerate(scene.get("choices", [])):
            key = f"{scene_id}#{i}"
            if ch.get("once") and key in self.state.used_choices:
                continue
            if self.check(ch.get("if")):
                result.append({**ch, "__key__": key})
            elif ch.get("show_locked"):
                result.append({**ch, "__locked__": True, "__key__": key})
        return result

    def _render_scene(self, scene_id: str, scene: dict) -> None:
        if self.meta.get("clear_screen", True):
            self.term.page_break()  # pause on unread output, then wipe
        title = scene.get("title", "")
        self.term.rule()
        header = f"[bold bright_white]{self._template(title)}[/]" if title else ""
        game_title = self.meta.get("title", "")
        if header:
            self.term.echo(header, wrap=False)
        elif game_title:
            self.term.echo(f"[bold]{game_title}[/]", wrap=False)
        self.term.rule()
        art = scene.get("art")
        if art:
            if isinstance(art, list):
                art = "\n".join(art)
            self.term.echo(art, wrap=False, markup=False)
            self.term.echo()
        text = scene.get("text", "")
        if text:
            self.term.echo(self._template(text))
        self.term.echo()

    def _prompt_choice(self, choices: list[dict]) -> dict | None:
        """Print numbered menu, read input, handle system commands.
        Returns the chosen choice dict, or None if the game should re-render."""
        for n, ch in enumerate(choices, 1):
            label = self._template(ch.get("text", "..."))
            if ch.get("__locked__"):
                reason = ch.get("locked_text", "locked")
                self.term.echo(f"  [dim]{n}. {label}  ({reason})[/]", wrap=False)
            else:
                self.term.echo(f"  [bold cyan]{n}.[/] {label}", wrap=False)
        self.term.echo()

        while True:
            raw = self.term.prompt("[bold magenta]> [/]").strip().lower()
            if not raw:
                continue
            parts = raw.split()
            cmd, args = parts[0], parts[1:]

            if cmd in ("inv", "i"):
                self._show_inventory()
            elif cmd in ("ach", "achievements"):
                self._show_achievements()
            elif cmd == "stats":
                self._show_stats()
            elif cmd == "help":
                self._show_help()
            elif cmd == "equip" and args:
                self._equip(" ".join(args))
            elif cmd == "unequip" and args:
                self._unequip_item(" ".join(args))
            elif cmd == "save":
                self._do_save(args[0] if args else "1")
            elif cmd == "load":
                if self._do_load(args[0] if args else "1"):
                    return None  # re-render from loaded state
            elif cmd in ("quit", "exit", "q"):
                if getattr(self.term, "eof", False):
                    self._running = False
                    return None
                confirm = self.term.prompt(
                    "[yellow]Quit without saving? (y/n) [/]").strip().lower()
                if confirm.startswith("y"):
                    self._running = False
                    return None
            elif cmd.isdigit():
                idx = int(cmd)
                if 1 <= idx <= len(choices):
                    chosen = choices[idx - 1]
                    if chosen.get("__locked__"):
                        self.term.echo("[dim]You can't do that yet.[/]")
                        continue
                    return chosen
                self.term.echo("[red]No such option.[/]")
            else:
                self.term.echo("[dim]Type a number, or 'help'.[/]")

    # ------------------------------------------------------------------ #
    # main loop
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        self._running = True
        try:
            while self._running:
                scene_id = self.state.current_scene
                scene = self.scenes.get(scene_id)
                if scene is None:
                    raise GameError(f"Unknown scene {scene_id!r}")

                first_visit = not self.state.was_visited(scene_id)
                self.state.mark_visited(scene_id)
                self.state.turn += 1

                # scene-level music
                if scene.get("music"):
                    self.audio.play_music(scene["music"])

                self._render_scene(scene_id, scene)

                # on_enter effects (on_first_enter runs only the first time)
                self._pending_goto = None
                if first_visit:
                    self.run_effects(scene.get("on_first_enter", []))
                self.run_effects(scene.get("on_enter", []))

                # terminal scene (an ending): stop before triggers can loop us
                if scene.get("end"):
                    self._check_achievements()
                    self._running = False
                    self.term.echo("\n[bold bright_yellow]— THE END —[/]",
                                   wrap=False)
                    break

                self._run_triggers()
                self._check_achievements()
                if not self._running:
                    break
                if self._pending_goto and self._pending_goto != scene_id:
                    self.state.current_scene = self._pending_goto
                    continue

                choices = self._available_choices(scene_id, scene)
                if not choices:
                    self._running = False
                    self.term.echo("\n[bold bright_yellow]— THE END —[/]",
                                   wrap=False)
                    break

                chosen = self._prompt_choice(choices)
                if chosen is None:
                    continue  # loaded a save, quit, or needs re-render
                if chosen.get("once"):
                    self.state.used_choices.append(chosen["__key__"])
                self.term.mark()  # everything printed from here is unread

                self.run_effects(chosen.get("do", []))
                if chosen.get("shop"):
                    self._run_shop(chosen["shop"])
                if chosen.get("dialogue"):
                    self._run_dialogue(chosen["dialogue"])
                self._check_achievements()
                self._run_triggers()
                if self._pending_goto:
                    self.state.current_scene = self._pending_goto
                elif chosen.get("goto"):
                    self.state.current_scene = chosen["goto"]
                # else: stay in the same scene (useful for "look around" choices)
        finally:
            self.audio.stop_music(fade_ms=300)
            self.audio.shutdown()
            self.term.echo("\n[dim]Thanks for playing.[/]", wrap=False)


# ---------------------------------------------------------------------- #
# CLI entry point:
#   python -m textquest game.json            play
#   python -m textquest --new  game.json     create a game (opens the editor)
#   python -m textquest --edit game.json     edit a game
#   python -m textquest --check game.json    validate
#   python -m textquest --map  game.json     draw the scene graph
# ---------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="textquest",
        description="Play, create, and inspect textquest games.")
    parser.add_argument("game", help="path to the game .json file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--new", action="store_true",
                      help="create a new game and open the editor")
    mode.add_argument("--edit", action="store_true",
                      help="open the interactive game editor")
    mode.add_argument("--check", action="store_true",
                      help="validate the game and report errors/warnings")
    mode.add_argument("--map", action="store_true", dest="show_map",
                      help="print the scene graph")
    parser.add_argument("--debug", action="store_true",
                        help="show engine warnings (bad expressions, etc.)")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--no-clear", action="store_true",
                        help="don't clear the screen between scenes")
    parser.add_argument("--seed", type=int, default=None,
                        help="RNG seed (reproducible runs)")
    args = parser.parse_args(argv)

    term = Terminal(use_color=False if args.no_color else None)

    if args.new or args.edit:
        from creator import Creator
        if args.new and os.path.isfile(args.game):
            term.echo(f"[red]{args.game} already exists — "
                      f"use --edit instead.[/]")
            return 1
        Creator(args.game, term=term).run()
        return 0

    if args.check or args.show_map:
        from tools import render_map, validate_game
        with open(args.game, encoding="utf-8") as f:
            game = json.load(f)
        if args.show_map:
            term.echo(render_map(game), wrap=False)
            return 0
        errors, warnings = validate_game(game)
        for e in errors:
            term.echo(f"[bold red]✗ {e}[/]", wrap=False)
        for w in warnings:
            term.echo(f"[yellow]⚠ {w}[/]", wrap=False)
        if not errors and not warnings:
            term.echo("[bold green]✓ No problems found.[/]", wrap=False)
        return 1 if errors else 0

    engine = Engine(args.game, terminal=term, debug=args.debug,
                    seed=args.seed)
    if args.no_clear:
        engine.meta["clear_screen"] = False
    engine.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
