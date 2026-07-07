"""
textquest.state
===============
All mutable game state lives here, as plain JSON-serializable data.
Because of that, save/load is trivial and deterministic.
"""

from __future__ import annotations

import json
import os
import time


class GameState:
    def __init__(self, initial_vars: dict | None = None, start_scene: str = ""):
        self.variables: dict = dict(initial_vars or {})
        self.inventory: dict[str, int] = {}          # item_id -> quantity
        self.visited: dict[str, int] = {}            # scene_id -> visit count
        self.fired_triggers: list[str] = []          # ids of 'once' triggers
        self.achievements: list[str] = []            # unlocked achievement ids
        self.shop_stock: dict[str, dict] = {}        # shop_id -> {item: left}
        self.equipped: dict[str, str] = {}           # slot -> item_id
        self.used_choices: list[str] = []            # spent 'once' choices
        self.current_scene: str = start_scene
        self.turn: int = 0

    # ------------------------------------------------------------------ #
    # inventory helpers
    # ------------------------------------------------------------------ #
    def give(self, item_id: str, qty: int = 1) -> None:
        self.inventory[item_id] = self.inventory.get(item_id, 0) + qty

    def take(self, item_id: str, qty: int = 1) -> bool:
        have = self.inventory.get(item_id, 0)
        if have < qty:
            return False
        remaining = have - qty
        if remaining:
            self.inventory[item_id] = remaining
        else:
            self.inventory.pop(item_id, None)
        return True

    def has(self, item_id: str, qty: int = 1) -> bool:
        return self.inventory.get(item_id, 0) >= qty

    def count(self, item_id: str) -> int:
        return self.inventory.get(item_id, 0)

    # ------------------------------------------------------------------ #
    # visit tracking
    # ------------------------------------------------------------------ #
    def mark_visited(self, scene_id: str) -> None:
        self.visited[scene_id] = self.visited.get(scene_id, 0) + 1

    def was_visited(self, scene_id: str) -> bool:
        return self.visited.get(scene_id, 0) > 0

    def visit_count(self, scene_id: str) -> int:
        return self.visited.get(scene_id, 0)

    # ------------------------------------------------------------------ #
    # serialization
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "variables": self.variables,
            "inventory": self.inventory,
            "visited": self.visited,
            "fired_triggers": self.fired_triggers,
            "achievements": self.achievements,
            "shop_stock": self.shop_stock,
            "equipped": self.equipped,
            "used_choices": self.used_choices,
            "current_scene": self.current_scene,
            "turn": self.turn,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GameState":
        st = cls()
        st.variables = dict(data.get("variables", {}))
        st.inventory = dict(data.get("inventory", {}))
        st.visited = dict(data.get("visited", {}))
        st.fired_triggers = list(data.get("fired_triggers", []))
        st.achievements = list(data.get("achievements", []))
        st.shop_stock = dict(data.get("shop_stock", {}))
        st.equipped = dict(data.get("equipped", {}))
        st.used_choices = list(data.get("used_choices", []))
        st.current_scene = data.get("current_scene", "")
        st.turn = int(data.get("turn", 0))
        return st

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> "GameState":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
