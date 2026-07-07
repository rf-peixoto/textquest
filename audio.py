"""
textquest.audio
===============
Music and sound effects.

Backend: pygame.mixer if installed (`pip install pygame`), otherwise a silent
no-op backend — the engine and games keep working with no audio hardware,
no pygame, or in CI. Games never talk to pygame directly; they just say
`music dungeon.ogg` or `sfx door.wav` and this module resolves the file
relative to the game's `assets/audio/` folder.
"""

from __future__ import annotations

import os


class AudioManager:
    def __init__(self, asset_dir: str | None = None, enabled: bool = True):
        self.asset_dir = asset_dir or "."
        self.enabled = enabled
        self.backend = None
        self.current_music: str | None = None
        self._sfx_cache: dict[str, object] = {}
        if enabled:
            self._init_backend()

    # ------------------------------------------------------------------ #
    def _init_backend(self) -> None:
        try:
            os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
            import pygame  # type: ignore
            pygame.mixer.init()
            self.backend = pygame
        except Exception:
            self.backend = None  # silent fallback

    @property
    def available(self) -> bool:
        return self.backend is not None

    def _resolve(self, filename: str) -> str | None:
        for candidate in (
            os.path.join(self.asset_dir, filename),
            os.path.join(self.asset_dir, "audio", filename),
            filename,
        ):
            if os.path.isfile(candidate):
                return candidate
        return None

    # ------------------------------------------------------------------ #
    # music (long, looping background tracks)
    # ------------------------------------------------------------------ #
    def play_music(self, filename: str, loop: bool = True,
                   volume: float = 0.7) -> None:
        if not self.backend:
            return
        if self.current_music == filename:
            return  # already playing; don't restart on scene re-entry
        path = self._resolve(filename)
        if not path:
            return
        try:
            self.backend.mixer.music.load(path)
            self.backend.mixer.music.set_volume(volume)
            self.backend.mixer.music.play(-1 if loop else 0)
            self.current_music = filename
        except Exception:
            pass

    def stop_music(self, fade_ms: int = 800) -> None:
        if not self.backend:
            self.current_music = None
            return
        try:
            self.backend.mixer.music.fadeout(fade_ms)
        except Exception:
            pass
        self.current_music = None

    # ------------------------------------------------------------------ #
    # sound effects (short, fire-and-forget)
    # ------------------------------------------------------------------ #
    def play_sfx(self, filename: str, volume: float = 1.0) -> None:
        if not self.backend:
            return
        path = self._resolve(filename)
        if not path:
            return
        try:
            sound = self._sfx_cache.get(path)
            if sound is None:
                sound = self.backend.mixer.Sound(path)
                self._sfx_cache[path] = sound
            sound.set_volume(volume)
            sound.play()
        except Exception:
            pass

    def shutdown(self) -> None:
        if self.backend:
            try:
                self.backend.mixer.quit()
            except Exception:
                pass
