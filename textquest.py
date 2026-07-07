#!/usr/bin/env python3
"""
textquest.py — one entry point for everything
=============================================

    python3 textquest.py games/demo.json           play
    python3 textquest.py --new  mygame.json        create (opens the editor)
    python3 textquest.py --edit mygame.json        edit
    python3 textquest.py --check mygame.json       validate
    python3 textquest.py --map  mygame.json        draw the scene graph

Every module can also be run directly:
    python3 engine.py  games/demo.json             play
    python3 creator.py mygame.json                 create/edit
"""

import sys

from engine import main

if __name__ == "__main__":
    sys.exit(main())
