"""An ARIA snapshot of a page in Playwright's YAML format, built from Chrome's accessibility tree.

Each element gets a ref (`e12`, or `f1e12` in iframe `f1`) that tools resolve back to its DOM node. Compared with
Playwright's snapshot, unnamed wrapper `generic`s that are neither clickable nor focusable are left out and their
children hoisted, inline formatting (`strong`, `emphasis`, ...) is merged into the surrounding text, and decorative
nodes are dropped, so snapshots are a fraction of the size with the same refs to act on.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

HOIST = {"none", "generic", "LabelText", "Section", "MenuListPopup", "LayoutTable", "LayoutTableRow", "LayoutTableCell",
         "Pre", "Ruby", "RubyAnnotation", "SvgRoot", "Unknown", "DescriptionListDetail", "group"}
INLINE = {"strong", "emphasis", "subscript", "superscript", "mark", "time", "Abbr", "deletion", "insertion", "Mark",
          "emphasis", "Emphasis", "Strong", "ruby"}
DROP = {"InlineTextBox", "ListMarker", "LineBreak", "Legend"}
ROLES = {"image": "img", "Iframe": "iframe", "IframePresentational": "iframe", "Canvas": "canvas",
         "DescriptionList": "list", "DescriptionListTerm": "term", "DescriptionListDetail": "definition",
         "Figcaption": "caption", "Video": "video", "Audio": "audio", "Details": "group", "DisclosureTriangle": "button",
         "PdfRoot": "document", "RootWebArea": "document", "WebArea": "document", "Code": "code"}
VALUED = {"textbox", "searchbox", "combobox", "spinbutton", "slider", "TextField", "textField"}
PROPS = ["checked", "disabled", "expanded", "level", "pressed", "selected"]  # Shown as [name] or [name=value].
MAX_NAME, MAX_TEXT, MAX_URL = 300, 2000, 300
REF = re.compile(r"^(f\d+)?e\d+$")


@dataclass
class Item:
    role: str
    name: str = ""
    ref: str | None = None
    props: list[str] = field(default_factory=list)
    text: str | None = None
    url: str | None = None
    cursor: bool = False
    box: tuple[int, int, int, int] | None = None
    children: list["Item | str"] = field(default_factory=list)
    key: tuple[str, int] | None = None  # (frame prefix, backendNodeId) the ref is made from.


@dataclass
class Frame:
    """One frame's accessibility tree with what the snapshot needs from its DOM."""

    prefix: str  # "" for the main frame, else e.g. "f1".
    nodes: list[dict]
    cursors: dict[int, str] = field(default_factory=dict)  # backendNodeId -> computed `cursor`.
    displays: dict[int, str] = field(default_factory=dict)  # backendNodeId -> computed `display`.
    boxes: dict[int, tuple[float, float, float, float]] = field(default_factory=dict)  # backendNodeId -> viewport box.
    children: dict[int, "Frame"] = field(default_factory=dict)  # iframe backendNodeId -> its frame.


RefFor = Callable[[str, int], str]


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def build(frame: Frame, ref_for: RefFor, assign: bool = True) -> list[Item | str]:
    """The top-level items of `frame`'s snapshot, with refs numbered in document order."""
    by_id = {node["nodeId"]: node for node in frame.nodes}
    roots = [node for node in frame.nodes if not node.get("parentId") or node["parentId"] not in by_id]
    items: list[Item | str] = []
    for root in roots:
        items += _convert(frame, by_id, root, ref_for, parent_cursor="auto", depth=0)
    items = _merge(items)
    if assign:
        for item in walk_items(items):
            if item.key:
                item.ref = ref_for(*item.key)
    return items


def _value(node: dict, key: str) -> str:
    value = node.get(key, {}).get("value")
    return "" if value is None else str(value)


def _properties(node: dict) -> dict:
    return {prop["name"]: prop.get("value", {}).get("value") for prop in node.get("properties", [])}


def _convert(frame: Frame, by_id: dict, node: dict, ref_for: RefFor, parent_cursor: str, depth: int) -> list[Item | str]:
    if depth > 400:  # Pathologically deep pages.
        return []
    role = _value(node, "role")
    backend = node.get("backendDOMNodeId")
    cursor = frame.cursors.get(backend, parent_cursor) if backend is not None else parent_cursor
    if role in DROP:
        return []
    if role == "StaticText":
        text = squash(_value(node, "name"))
        return [text] if text else []

    children: list[Item | str] = []
    if role in ("Iframe", "IframePresentational") and backend in frame.children:
        children = build(frame.children[backend], ref_for, assign=False)
    else:
        for child_id in node.get("childIds", []):
            if child := by_id.get(child_id):
                children += _convert(frame, by_id, child, ref_for, cursor, depth + 1)
    children = _merge(children)

    if node.get("ignored"):
        return children
    properties = _properties(node)
    name = squash(_value(node, "name"))
    clickable = cursor == "pointer" and parent_cursor != "pointer"
    interesting = clickable or (properties.get("focusable") and role not in ("RootWebArea", "WebArea"))
    if role in ("RootWebArea", "WebArea"):
        return children
    if role in INLINE and not interesting:
        return children
    if role in ("code", "Code") and not interesting and all(isinstance(c, str) for c in children):
        return [f"`{' '.join(children)}`"] if children else []
    if role in HOIST and not name and not interesting:
        # A block of text, such as a <div> a script made draggable, keeps a ref; wrappers and inline spans do not.
        block = frame.displays.get(backend, "inline") not in ("inline", "contents", "none")
        if not (block and children and all(isinstance(c, str) for c in children) and role != "LabelText"):
            return children
    if role == "image" and not name and not interesting:
        return []
    if role == "paragraph" and not children and not name:
        return []

    item = Item(role=ROLES.get(role, role[:1].lower() + role[1:] if role[:1].isupper() else role),
                name=clip(name, MAX_NAME), cursor=clickable)
    if backend is not None:
        item.key = (frame.prefix, backend)
        if box := frame.boxes.get(backend):
            item.box = tuple(round(v) for v in box)
    for prop in PROPS:
        value = properties.get(prop)
        if value is None or value is False or value == "false":
            continue
        item.props.append(prop if value is True or value == "true" else f"{prop}={value}")
    if properties.get("focused") and role not in ("RootWebArea", "WebArea"):
        item.props.append("active")
    if role == "link" and (url := properties.get("url")):
        item.url = clip(str(url), MAX_URL)
    if role in VALUED and (value := squash(_value(node, "value"))):
        item.text = clip(value, MAX_TEXT)
    if role == "combobox" and any(isinstance(c, Item) for c in children):
        item.text = None  # A <select>: its listed options mark the chosen one as [selected].
    if all(isinstance(child, str) for child in children):
        text = clip(" ".join(children), MAX_TEXT)
        if text and text != item.name and item.text is None:
            item.text = text
        children = []
    item.children = children
    return [item]


def _merge(items: list[Item | str]) -> list[Item | str]:
    """Join adjacent text runs, and drop label text that only repeats the name of the field after it."""
    merged: list[Item | str] = []
    for item in items:
        if isinstance(item, str) and merged and isinstance(merged[-1], str):
            merged[-1] = squash(f"{merged[-1]} {item}")
        elif item != "":
            merged.append(item)
    return [item for i, item in enumerate(merged)
            if not (isinstance(item, str) and i + 1 < len(merged) and isinstance(merged[i + 1], Item) and merged[i + 1].name == item)]


_PLAIN = re.compile(r"^[^\s\-?:,\[\]{}#&*!|>'\"%@`]([^:#]|:(?!\s)|#(?<!\s#))*$")


def _scalar(text: str) -> str:
    """`text` as a YAML scalar, quoted only when it has to be."""
    if text and _PLAIN.match(text) and not text.endswith(":") and text not in ("true", "false", "null", "yes", "no"):
        return text
    return "'" + text.replace("'", "''") + "'"


def head(item: Item, boxes: bool = False) -> str:
    parts = [item.role]
    if item.name:
        parts.append('"' + item.name.replace("\\", "\\\\").replace('"', '\\"') + '"')
    parts += [f"[{prop}]" for prop in item.props]
    if item.ref:
        parts.append(f"[ref={item.ref}]")
    if item.cursor:
        parts.append("[cursor=pointer]")
    if boxes and item.box:
        parts.append("[box={},{},{},{}]".format(*item.box))
    return " ".join(parts)


@dataclass
class Line:
    indent: int
    text: str
    parent: int | None  # Index of the parent line.


def lines(items: list[Item | str], depth: int | None = None, boxes: bool = False) -> list[Line]:
    out: list[Line] = []

    def emit(entries: list[Item | str], indent: int, parent: int | None):
        for entry in entries:
            if isinstance(entry, str):
                out.append(Line(indent, f"- text: {_scalar(entry)}", parent))
                continue
            key = _scalar(head(entry, boxes))
            nested = bool(entry.children or entry.url)
            if not nested:
                out.append(Line(indent, f"- {key}" + (f": {_scalar(entry.text)}" if entry.text else ""), parent))
                continue
            if depth is not None and indent + 1 >= depth:  # Children cut off by `depth`.
                out.append(Line(indent, f"- {key}", parent))
                continue
            out.append(Line(indent, f"- {key}:", parent))
            index = len(out) - 1
            if entry.text is not None and nested:
                out.append(Line(indent + 1, f"- text: {_scalar(entry.text)}", index))
            if entry.url:
                out.append(Line(indent + 1, f"- /url: {_scalar(entry.url)}", index))
            emit(entry.children, indent + 1, index)

    emit(items, 0, None)
    return out


def render(items: list[Item | str], depth: int | None = None, boxes: bool = False) -> str:
    return "\n".join("  " * line.indent + line.text for line in lines(items, depth, boxes))


def find(items: list[Item | str], pattern: re.Pattern, max_results: int | None = None, context: int = 4) -> tuple[int, str]:
    """Matching lines, each under its path from the root and followed by a few lines of its subtree."""
    all_lines = lines(items)
    matches = [i for i, line in enumerate(all_lines) if pattern.search(line.text)]
    # A match inside another match's shown context is already visible there.
    blocks, shown = [], set()
    for index in matches:
        if index in shown:
            continue
        if max_results is not None and len(blocks) >= max_results:
            break
        path, parent = [], all_lines[index].parent
        while parent is not None:
            path.append(parent)
            parent = all_lines[parent].parent
        chosen = list(reversed(path)) + [index]
        following = index + 1
        while following < len(all_lines) and all_lines[following].indent > all_lines[index].indent and following - index <= context:
            chosen.append(following)
            following += 1
        shown.update(chosen)
        blocks.append("\n".join("  " * all_lines[i].indent + all_lines[i].text for i in chosen))
    return len(matches), "\n\n----\n\n".join(blocks)


def find_item(items: list[Item | str], ref: str) -> Item | None:
    for entry in items:
        if isinstance(entry, Item):
            if entry.ref == ref:
                return entry
            if found := find_item(entry.children, ref):
                return found
    return None


def walk_items(items: list[Item | str]):
    for entry in items:
        if isinstance(entry, Item):
            yield entry
            yield from walk_items(entry.children)
