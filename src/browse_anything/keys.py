"""Key definitions for `Input.dispatchKeyEvent`, on a US layout, in Playwright's key names."""

import sys
from dataclasses import dataclass

MODIFIERS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
CONTROL_OR_META = "Meta" if sys.platform == "darwin" else "Control"


@dataclass(frozen=True)
class Key:
    key: str
    code: str
    keycode: int
    text: str = ""
    location: int = 0


def _build() -> dict[str, Key]:
    keys: dict[str, Key] = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        keys[c] = Key(c, f"Key{c.upper()}", ord(c.upper()), c)
        keys[c.upper()] = Key(c.upper(), f"Key{c.upper()}", ord(c.upper()), c.upper())
    shifted_digits = ")!@#$%^&*("
    for d in range(10):
        keys[str(d)] = Key(str(d), f"Digit{d}", 48 + d, str(d))
        keys[shifted_digits[d]] = Key(shifted_digits[d], f"Digit{d}", 48 + d, shifted_digits[d])
    punctuation = [  # (unshifted, shifted, code, keyCode)
        (";", ":", "Semicolon", 186), ("=", "+", "Equal", 187), (",", "<", "Comma", 188), ("-", "_", "Minus", 189),
        (".", ">", "Period", 190), ("/", "?", "Slash", 191), ("`", "~", "Backquote", 192), ("[", "{", "BracketLeft", 219),
        ("\\", "|", "Backslash", 220), ("]", "}", "BracketRight", 221), ("'", '"', "Quote", 222),
    ]
    for plain, shifted, code, keycode in punctuation:
        keys[plain] = Key(plain, code, keycode, plain)
        keys[shifted] = Key(shifted, code, keycode, shifted)
    named = {
        "Enter": Key("Enter", "Enter", 13, "\r"), "Tab": Key("Tab", "Tab", 9), "Backspace": Key("Backspace", "Backspace", 8),
        "Delete": Key("Delete", "Delete", 46), "Escape": Key("Escape", "Escape", 27), " ": Key(" ", "Space", 32, " "),
        "ArrowLeft": Key("ArrowLeft", "ArrowLeft", 37), "ArrowUp": Key("ArrowUp", "ArrowUp", 38),
        "ArrowRight": Key("ArrowRight", "ArrowRight", 39), "ArrowDown": Key("ArrowDown", "ArrowDown", 40),
        "Home": Key("Home", "Home", 36), "End": Key("End", "End", 35), "PageUp": Key("PageUp", "PageUp", 33),
        "PageDown": Key("PageDown", "PageDown", 34), "Insert": Key("Insert", "Insert", 45),
        "CapsLock": Key("CapsLock", "CapsLock", 20), "ContextMenu": Key("ContextMenu", "ContextMenu", 93),
        "Shift": Key("Shift", "ShiftLeft", 16, location=1), "Control": Key("Control", "ControlLeft", 17, location=1),
        "Alt": Key("Alt", "AltLeft", 18, location=1), "Meta": Key("Meta", "MetaLeft", 91, location=1),
    }
    named |= {f"F{n}": Key(f"F{n}", f"F{n}", 111 + n) for n in range(1, 13)}
    keys |= named
    aliases = {"Space": " ", "Esc": "Escape", "Return": "Enter", "Del": "Delete", "Left": "ArrowLeft", "Right": "ArrowRight",
               "Up": "ArrowUp", "Down": "ArrowDown", "Cmd": "Meta", "Command": "Meta", "Ctrl": "Control", "Option": "Alt"}
    keys |= {alias: keys[name] for alias, name in aliases.items()}
    return keys


KEYS = _build()

# Editing commands Chrome does not derive from shortcuts on macOS without a native window.
MAC_COMMANDS = {"a": "selectAll", "c": "copy", "x": "cut", "v": "paste", "z": "undo"}


def parse(combo: str) -> tuple[list[str], Key]:
    """`Control+Shift+a` -> (["Control", "Shift"], Key(a)). A lone `+` is the plus key."""
    parts = combo.split("+") if combo != "+" else ["+"]
    if parts[-1] == "" and len(parts) > 1:  # "Control++"
        parts = [*parts[:-2], "+"]
    *modifiers, name = parts
    modifiers = [CONTROL_OR_META if m == "ControlOrMeta" else KEYS[m].key if m in KEYS else m for m in modifiers]
    for modifier in modifiers:
        if modifier not in MODIFIERS:
            raise ValueError(f"unknown modifier {modifier!r} in {combo!r}")
    if name == "ControlOrMeta":
        name = CONTROL_OR_META
    if name in KEYS:
        return modifiers, KEYS[name]
    if len(name) == 1:  # Any other character: typed as text.
        return modifiers, Key(name, "", 0, name)
    raise ValueError(f"unknown key {name!r}")


def mask(modifiers) -> int:
    return sum(MODIFIERS[m] for m in set(modifiers))
