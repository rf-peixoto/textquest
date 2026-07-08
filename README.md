# textquest

A data-driven engine **and authoring studio** for choice-based terminal
games, in pure Python. Write games in any genre — mystery, sci-fi, horror,
romance, survival, fantasy — without writing code: games are data, and the
editor writes the data for you.

```
textquest/
├── textquest.py    # one entry point for everything (play/new/edit/check/map)
├── engine.py       # game loop, scenes, choices, effects, triggers
├── creator.py      # the interactive editor — also runnable on its own
├── tools.py        # validator & scene-graph map
├── state.py        # variables, inventory, visits, save/load
├── dsl.py          # safe expression evaluator (conditions & dice)
├── ui.py           # colors, markup, wrapping, screen paging
├── audio.py        # music & SFX (pygame if installed, silent otherwise)
├── games/
│   ├── demo.json       # "The Cavern of Echoes" — fantasy adventure
│   └── derelict.json   # "The Derelict" — sci-fi mystery (same engine!)
└── assets/audio/   # drop .ogg / .wav files here
```

All files live in one folder with plain imports — run any of them directly.
No installation, no packages, no dependencies (pygame is optional, for
audio only).

## Commands

```bash
python3 textquest.py games/demo.json        # play
python3 textquest.py --new  mygame.json     # create a game (opens editor)
python3 textquest.py --edit mygame.json     # edit a game
python3 textquest.py --check mygame.json    # validate: errors & warnings
python3 textquest.py --map  mygame.json     # draw the scene graph
python3 textquest.py --autoplay 50 game.json # bot-play 50 times: crashes,
                                             # endings, unreached content

python3 creator.py                          # editor, asks for a filename
python3 creator.py mygame.json              # creates if new, edits if not
python3 engine.py  games/derelict.json      # playing works directly too
```

Play flags: `--debug` (show engine warnings), `--no-color`, `--no-clear`
(don't wipe the screen between scenes), `--seed N` (reproducible dice),
`--start SCENE` (begin anywhere — playtesting). Colors and unicode degrade
gracefully: Windows terminals get VT mode enabled automatically, and every
symbol has an ASCII fallback when the console can't render unicode.

While playing, the screen is wiped between scenes for immersion — but never
before you've read what's on it: whenever effects, dice rolls, or messages
appear, the engine waits for Enter before clearing. In-game commands:
choice numbers, `inv`/`i`, `stats`, `ach`, `equip <item>` / `unequip
<item>`, `back` (undo your last move — up to 60 steps), `save [slot]`,
`load [slot]`, `help`, `quit`. The engine autosaves every scene, so
`load auto` resumes after a crash or an accidental quit; saves are stamped
with the game's title and content hash, and loading warns if the game file
has changed since the save was made.

## Creating a game (start here)

```bash
python3 creator.py mygame.json
```

The wizard asks for a title, an author, and a **genre template** — a
starting variable kit and a sample opening line for fantasy, mystery,
sci-fi, horror, slice-of-life/romance, survival, or blank. Templates are
only a head start: *the engine has no genre*. Variables are whatever you
name them (`sanity`, `credits`, `affection`, `suspicion`), shops trade in
any variable you declare as their currency (a noir bribes-for-favors
economy works exactly like a fantasy gold shop), and dice checks read the
same whether they're sword swings or hacking attempts.

Then you're in the editor: a menu system covering scenes, choices, items
(with equipment slots), variables, shops, dialogues, achievements, global
triggers, and settings. The workflow that works:

1. Open **scenes**, pick your start scene, write its text.
2. Add **choices**. When a choice's `goto` names a scene that doesn't exist,
   the editor offers to create a `[draft]` stub instantly — say yes and keep
   going. Sketch the whole story graph first; write prose later. Drafts are
   flagged in the scene list and by the validator.
3. **show map** to see the story's shape, **validate** to catch broken
   links, **save & playtest** to try it immediately (quit the playtest and
   you're back in the editor). A built-in **help** screen recaps all of
   this plus an effects cheatsheet (also available by typing `?` at any
   effects prompt).

`--check` separates **errors**: on top of structural checks, it now
statically validates *every expression in the game* — each `if`, every
`set`/`roll`/`contest` right-hand side, even `{if ...}` blocks inside
prose — catching syntax errors, unknown functions, and references to
variables that are never defined or assigned anywhere. A `helth > 5` typo
is caught at check time, not mid-playthrough. It also separates (anything that will break at runtime: gotos
to missing scenes, shops stocking undefined items, dialogue responses to
missing nodes) from **warnings** (design smells: unreachable scenes, silent
dead ends, wearable modifiers on slot-less items). Exit code 1 on errors,
so it works in scripts. `--map` draws the graph, marking endings, shops,
conversations, cycles `(…)`, trigger-only scenes, and unreachables.

## Anatomy of a game file

The editor writes all of this for you, but it's plain JSON if you'd rather
type:

```json
{
  "meta": { "title": "My Game", "start": "intro", "clear_screen": true },
  "variables": { "suspicion": 0, "clues": 0 },
  "items": {}, "shops": {}, "dialogues": {}, "achievements": {},
  "triggers": [
    { "id": "busted", "if": "suspicion >= 10", "once": false,
      "goto": "arrested" }
  ],
  "scenes": {
    "intro": {
      "title": "The Scene of the Crime",
      "art": "  _____\n |     |\n |_____|",
      "text": "You have [cyan]{clues}[/] clues so far.",
      "music": "rain.ogg",
      "on_first_enter": ["ask detective = What's your name, detective?"],
      "choices": [
        { "text": "Examine the body", "goto": "morgue",
          "do": ["set clues = clues + 1"], "once": true },
        { "text": "Question the widow", "dialogue": "widow",
          "if": "clues >= 1", "show_locked": true,
          "locked_text": "find something to ask about first" }
      ]
    },
    "arrested": { "end": true, "text": "Wrong house, detective." }
  }
}
```

A scene needs only `text`. Optional: `title`, `art` (ASCII art shown
verbatim above the text — brackets and all, no markup mangling), `music`,
`on_first_enter` / `on_enter` effect lists, `choices`, and `end`. A choice
without `goto` re-renders the same scene (repeatable actions); `once: true`
makes a choice vanish forever after one use (searching a drawer, a
one-time favor) — and stays used across save/load.

### Effects — the verbs

One-line commands run in order from choices, scene entries, triggers,
dialogue nodes, shop transactions, or achievement rewards:

| Effect | Meaning |
|---|---|
| `set fear = fear + 1` | assign any variable (creates it if new) |
| `roll check = 1d20 + skill` | visible dice roll stored in a variable |
| `ask name = What's your name?` | store the player's typed answer (numbers become ints) |
| `give keycard` / `give ration 3` | inventory in |
| `take keycard` / `take ration 2` | inventory out (auto-unequips gear) |
| `say The lights die one by one.` | print a line (markup + `{vars}`) |
| `goto cellar` | jump to a scene |
| `random_goto lucky*3 unlucky` | weighted random jump (`*N` = weight) |
| `call take_damage` | run a named macro (shared effect block) |
| `draw cave_loot` | roll on a weighted outcome table |
| `contest duel = 1d20 + skill vs guard` | opposed roll vs an NPC stat block |
| `shop vending` / `dialogue widow` | open a shop / conversation |
| `equip multitool` / `unequip multitool` | wear / remove gear |
| `unlock case_closed` | grant an achievement manually |
| `music theme.ogg` / `music stop` / `sfx door.wav` | audio |
| `pause` | wait for Enter (dramatic beats) |
| `end` | stop the game |

An effect can also be a conditional block —
`{"if": "roll_result >= 15", "do": [...], "else": [...]}` — which is the
whole pattern for skill checks and branching consequences (the editor has a
guided form for these: just start an effect line with `if <condition>`).

**Macros** (`"macros": {"take_damage": [...]}`) are shared effect blocks —
define damage handling or time passing once, `call` it everywhere.
**Tables** (`"tables": {"cave_loot": [{"weight": 3, "do": [...]}, ...]}`)
are weighted outcome pools for loot, encounters, and random events; entries
can carry `if` conditions. **NPCs** (`"npcs": {"guard": {"name": "Guard",
"roll": "1d20 + 4"}}`) are reusable opponents: `contest duel = 1d20 + skill
vs guard` rolls both sides visibly and stores `duel` (your total minus
theirs — win if `> 0`, tie if `== 0`), plus `duel_you` / `duel_them` for
flavor text.

### Conditions — the questions

Safe Python-like expressions (AST-whitelisted; game files can never run
code): all your variables by name, plus `has('item')`, `count('item')`,
`equipped('item')`, `visited('scene')`, `visits('scene')`, `turn()`,
`chance(0.25)`, `randint(1,6)`, silent dice `d4()…d100()` and
`roll('2d6+1')`, and `min`/`max`/`abs`/`int`/`len`.

### Dice — roll and compare, anywhere

`roll tension = 2d6 + nerve` prints the actual faces
(`🎲 tension: 2d6[4,1] + nerve = 8`) and stores the total; compare it in
any condition. Works for combat, lockpicking, interrogation, seduction,
navigation — the primitive doesn't care. Silent in-condition rolls
(`"if": "d20() >= 15"`) cover hidden checks.

### Shops, dialogue, equipment, achievements

**Shops** are defined once and opened from any choice (`"shop": "vending"`)
or effect. The engine renders the trade menu, checks the currency (any
variable), enforces limited stock — tracked per stock entry, persisted in
saves — handles buyback via a `"buys"` map, and runs `on_buy`/`on_sell`
effects. **Dialogues** are node trees: an NPC line plus player responses
with `if`/`do`/`goto`/`exit`; a response without `goto` repeats the node,
a node without visible responses ends the talk with the NPC's last word.
**Equipment**: items with a `slot` (any word) and `modifiers` that adjust
variables while worn — slots auto-swap, stats never leak, and since
modifiers touch plain variables they boost every roll and condition
naturally. **Achievements** auto-unlock when their `if` turns true, or
manually via `unlock`; `secret: true` hides them until earned; `reward`
effects run once; `ach` shows progress.

### Living prose

Scenes can carry a `revisit_text` shown on every visit after the first —
the cheap trick that makes places feel remembered rather than static. And
any text supports inline conditionals, nesting included:

```
{if has('torch')}The torchlight steadies you.{else}You grope forward in
the dark{if fear > 5}, and something gropes back{end}.{end}
```

### Text markup and templating

`{variable}` substitution plus `[tag]...[/]` styling in any text: colors
(`red`, `cyan`, `bright_yellow`, …), backgrounds (`on_red`), styles
(`bold`, `dim`, `italic`, `underline`). Tags nest; `[/]` closes the most
recent. Colors auto-disable when output isn't a terminal. Stray braces in
prose are left alone rather than crashing, and `art` is always printed
verbatim.

## Two demos, two genres, one engine

`games/demo.json` is a fantasy adventure (shop haggling, a troll, a d20
sneak). `games/derelict.json` is a sci-fi mystery: your *name* is an `ask`
prompt, oxygen is a countdown enforced by a repeatable trigger, the vending
machine trades in credits, a multitool is `hand`-slot equipment boosting
`tech`, a noise in the dark is a `random_goto`, clue-gated dialogue makes
the ship's AI confess, and the ending changes with your clue count. Diff
the two files: the engine parts are identical; only the nouns changed.

## Autoplay — the fuzzer that playtests for you

`--autoplay 50` unleashes a bot on your game fifty times: it picks random
choices, chats with NPCs, shops badly, answers `ask` prompts with nonsense,
and reports back — crashes (with the seed to replay them), which endings
were reached and how often, scenes never visited, achievements never
unlocked, and sessions that hit the 400-turn cap (loop suspects). Run it
after every writing session; it's the difference between "I think act two
works" and knowing. It found real design facts about the demos on its
first run: two achievements almost no player will stumble into.

## The editor is faster than code

That's the design bar. The tools that clear it: **jump to scene** by id or
prefix from the main menu or scene list; **find text anywhere** across
scenes, choices, and dialogue with jump-to-result; **playtest from any
scene** with setup effects (`set gold = 50`, `give torch`, `equip charm`) —
test act three without replaying act one; **guided conditional effects**
(type `if health <= 0` at any effects prompt and it walks you through
then/else); **prose export/import** — dump every scene's text to a `.txt`,
rewrite it in your own editor, import it back; a `.bak` backup before
every save; and invisible **stable ids** on one-time choices and triggers
so reordering content in the editor never corrupts a player's save.

## Ideas for where to take it next

Timed events via `turn()`, reusable enemy stat blocks, localization tables,
an export-to-single-file build for sharing games, and — when you're feeling
brave — a parser-based input layer on top of the same world model.
