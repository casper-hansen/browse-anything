"""An ARIA snapshot of a page in Playwright's YAML format, built from Chrome's accessibility tree.

Each element gets a ref (`e12`) that tools resolve back to its DOM node, in whichever frame it is: refs are numbered
across frames, so no two look alike. Compared with
Playwright's snapshot, unnamed wrapper `generic`s that are neither clickable nor focusable are left out and their
children hoisted, inline formatting (`strong`, `emphasis`, ...) is merged into the surrounding text, and decorative
nodes are dropped, so snapshots are a fraction of the size with the same refs to act on.
"""

import base64
import re
from collections.abc import Callable
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
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
# Roles that are clickable by nature, so [cursor=pointer] tells nothing about them.
INTERACTIVE = {"link", "button", "checkbox", "radio", "switch", "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
               "option", "combobox", "textbox", "searchbox", "slider", "spinbutton", "treeitem", "DisclosureTriangle",
               "ListBoxOption", "MenuListOption", "PopUpButton", "ToggleButton"}
MAX_NAME, MAX_TEXT = 300, 2000
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
    # Set by build() from the frame's root: links to its own origin are shown as paths.
    cursors: dict[int, str] = field(default_factory=dict)  # backendNodeId -> computed `cursor`.
    displays: dict[int, str] = field(default_factory=dict)  # backendNodeId -> computed `display`.
    boxes: dict[int, tuple[float, float, float, float]] = field(default_factory=dict)  # backendNodeId -> viewport box.
    children: dict[int, "Frame"] = field(default_factory=dict)  # iframe backendNodeId -> its frame.
    url: str = ""


RefFor = Callable[[str, int], str]


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


INVISIBLE = re.compile("[\u00ad\u200b-\u200d\u2060\ufeff]")  # Soft hyphens and zero-width characters.


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", INVISIBLE.sub("", text)).strip()


def build(frame: Frame, ref_for: RefFor, assign: bool = True) -> list[Item | str]:
    """The top-level items of `frame`'s snapshot, with refs numbered in document order."""
    by_id = {node["nodeId"]: node for node in frame.nodes}
    roots = [node for node in frame.nodes if not node.get("parentId") or node["parentId"] not in by_id]
    for root in roots:
        if _value(root, "role") in ("RootWebArea", "WebArea") and (url := _properties(root).get("url")):
            frame.url = str(url)
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
    if role == "listitem" and not name and not interesting and len(children) == 1 and isinstance(children[0], Item):
        return children  # A list entry that is one link or button: the entry itself adds nothing.
    # A wrapper inside a button or link that only repeats its name.
    children = [c for c in children if not (isinstance(c, Item) and c.role == "generic" and not c.name and not c.children
                                            and not c.cursor and c.text == name and name)]

    item = Item(role=ROLES.get(role, role[:1].lower() + role[1:] if role[:1].isupper() else role),
                name=clip(name, MAX_NAME), cursor=clickable and role not in INTERACTIVE)
    if backend is not None:
        item.key = (frame.prefix, backend)
        if box := frame.boxes.get(backend):
            item.box = tuple(round(v) for v in box)
    for prop in PROPS:
        value = properties.get(prop)
        if value is None or value is False or value == "false" or (prop == "level" and role != "heading"):
            continue
        item.props.append(prop if value is True or value == "true" else f"{prop}={value}")
    if properties.get("focused") and role not in ("RootWebArea", "WebArea"):
        item.props.append("active")
    if role == "link" and (url := properties.get("url")):
        item.url = relative(unwrap(str(url)), frame.url)
    if role in VALUED and (value := squash(_value(node, "value"))):
        item.text = clip(value, MAX_TEXT)
    if role == "combobox" and any(isinstance(c, Item) for c in children):
        item.text = None  # A <select>: its listed options mark the chosen one as [selected].
        children = _fold_options(children)
    if all(isinstance(child, str) for child in children):
        text = clip(" ".join(children), MAX_TEXT)
        if text and text != item.name and item.text is None:
            item.text = text
        children = []
    item.children = children
    return [item]


def unwrap(url: str, depth: int = 0) -> str:
    """The destination of a search engine's redirect link, which is what a reader needs to know; else `url`.

    URLs are otherwise shown whole, since a cut-off URL cannot be followed. Inline `data:` URLs are abbreviated.
    """
    if url.startswith("data:"):
        return url.split(",", 1)[0] + ",…" if len(url) > 100 else url
    try:
        if url.startswith("/"):  # A redirect link relative to the search engine's own page.
            return url
        parts = urlsplit(url)
        host, query = parts.netloc.lower(), parse_qs(parts.query)
        if host.endswith("bing.com") and parts.path in ("/ck/a", "/aclick") and (u := query.get("u", [""])[0]):
            encoded = u[2:] if parts.path == "/ck/a" and u.startswith("a1") else u
            target = unquote(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode())
        elif re.match(r"(www\.)?google\.[a-z.]+$", host) and parts.path == "/url":
            target = (query.get("q") or query.get("url") or [""])[0]
        elif host.endswith("duckduckgo.com") and parts.path == "/l/":
            target = query.get("uddg", [""])[0]
        elif host.endswith("duckduckgo.com") and parts.path == "/y.js":  # An ad: it wraps the ad network's redirect.
            target = query.get("u3", [""])[0]
        else:
            return url
    except (ValueError, UnicodeDecodeError):
        return url
    if target.startswith("/"):  # Bing's own pages, such as its shopping results.
        target = urlunsplit((parts.scheme, parts.netloc, "", "", "")) + target
    if not re.match(r"https?://", target):
        return url
    return untrack(unwrap(target, depth + 1)) if depth < 3 else target


# Click-tracking parameters ads add to their destination; the page is the same without them.
TRACKING = re.compile(r"^(utm_[a-z]+|msclkid|gclid|gbraid|wbraid|fbclid|dclid|yclid|mc_cid|mc_eid|_hsenc|_hsmi|srsltid)$")


def untrack(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [pair for pair in parts.query.split("&") if not TRACKING.match(unquote(pair.split("=", 1)[0]))]
    return urlunsplit(parts._replace(query="&".join(kept)))


SHOWN_OPTIONS = 10


def _fold_options(children: list[Item | str]) -> list[Item | str]:
    """A long <select>'s options after the first few, and the chosen ones, as one line of their names. They need no
    refs: browser_select_option picks an option by its text. A country list becomes a line instead of 250."""
    options = [c for c in children if isinstance(c, Item) and c.role == "option"]
    if len(options) <= SHOWN_OPTIONS + 2:
        return children
    folded = [o for o in options[SHOWN_OPTIONS:] if "selected" not in o.props]
    kept = [c for c in children if not (isinstance(c, Item) and c in folded)]
    return kept + [f"{len(folded)} more options, chosen by their text with browser_select_option: " + ", ".join(o.name for o in folded)]


def relative(url: str, base: str) -> str:
    """`url` as a path when it is on `base`'s origin, which is how most of a page's links read shortest."""
    target, page = urlsplit(url), urlsplit(base)
    if not page.netloc or (target.scheme, target.netloc) != (page.scheme, page.netloc):
        return url
    if (target.path, target.query) == (page.path, page.query) and target.fragment:
        return "#" + target.fragment
    return urlunsplit(("", "", target.path or "/", target.query, target.fragment))


def _merge(items: list[Item | str]) -> list[Item | str]:
    """Join adjacent text runs, and drop label text that only repeats the name of the field after it."""
    merged: list[Item | str] = []
    for item in items:
        if isinstance(item, str) and merged and isinstance(merged[-1], str):
            merged[-1] = squash(f"{merged[-1]} {item}")
        elif isinstance(item, Item) and item.role == "img" and not item.children and merged and isinstance(merged[-1], Item) \
                and (merged[-1].role, merged[-1].name) == ("img", item.name) and not merged[-1].children:
            continue  # The same picture again, such as a carousel's slides.
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


def find(items: list[Item | str], pattern: re.Pattern, max_results: int | None = None, context: int = 4) -> tuple[int, int, str]:
    """The snapshot cut down to the matching lines: one tree of each match under its path from the root, followed by
    a few lines of its subtree. Paths that matches share are shown once, and the links on a path keep their URL.

    Returns the number of matches, how many are shown, and the tree.
    """
    all_lines = lines(items)
    matches = [i for i, line in enumerate(all_lines) if pattern.search(line.text)]
    urls = {line.parent: i for i, line in enumerate(all_lines) if line.text.startswith("- /url: ")}
    chosen: set[int] = set()
    shown = 0
    for index in matches:
        if index in chosen:  # Already visible in another match's context.
            shown += 1
            continue
        if max_results is not None and shown >= max_results:
            break
        shown += 1
        parent = all_lines[index].parent
        while parent is not None and parent not in chosen:
            chosen.add(parent)
            if parent in urls:
                chosen.add(urls[parent])
            parent = all_lines[parent].parent
        chosen.add(index)
        following = index + 1
        while following < len(all_lines) and all_lines[following].indent > all_lines[index].indent and following - index <= context:
            chosen.add(following)
            following += 1
    return len(matches), shown, "\n".join("  " * all_lines[i].indent + all_lines[i].text for i in sorted(chosen))


def find_item(items: list[Item | str], ref: str) -> Item | None:
    for entry in items:
        if isinstance(entry, Item):
            if entry.ref == ref:
                return entry
            if found := find_item(entry.children, ref):
                return found
    return None


def find_roles(items: list[Item | str], roles: set[str], skip: Item | None = None) -> list[Item]:
    """The outermost items with one of `roles`, not looking inside `skip`."""
    found = []
    for entry in items:
        if not isinstance(entry, Item) or entry is skip:
            continue
        if entry.role in roles:
            found.append(entry)
        else:
            found += find_roles(entry.children, roles, skip)
    return found


CONTROLS = {"button", "link", "checkbox", "radio", "switch", "menuitem", "tab", "option", "combobox", "textbox"}


def controls(item: Item, limit: int = 8) -> list[Item]:
    """The first controls inside `item`, such as a cookie banner's buttons."""
    return [entry for entry in walk_items(item.children) if entry.role in CONTROLS and entry.ref][:limit]


def walk_items(items: list[Item | str]):
    for entry in items:
        if isinstance(entry, Item):
            yield entry
            yield from walk_items(entry.children)
