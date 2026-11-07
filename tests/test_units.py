import re

import pytest

from browse_anything import keys
from browse_anything.browser import is_shell
from browse_anything.session import Message, Request, is_local, remote_value
from browse_anything.snapshot import Frame, build, find, lines, render
from browse_anything.tools import TOOLS

# Playwright MCP's tool names, which browse-anything keeps so it can stand in for it.
PLAYWRIGHT_TOOLS = {
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_find", "browser_click", "browser_type",
    "browser_fill_form", "browser_select_option", "browser_hover", "browser_drag", "browser_drop", "browser_press_key",
    "browser_wait_for", "browser_take_screenshot", "browser_evaluate", "browser_tabs", "browser_handle_dialog",
    "browser_file_upload", "browser_console_messages", "browser_network_requests", "browser_network_request",
    "browser_resize", "browser_close", "browser_emulate_media", "browser_mouse_click_xy", "browser_mouse_move_xy",
    "browser_mouse_drag_xy", "browser_mouse_wheel",
}


def node(id, role, name=None, children=(), backend=None, ignored=False, properties=None, value=None, parent=None):
    n = {"nodeId": str(id), "role": {"value": role}, "childIds": [str(c) for c in children], "ignored": ignored}
    if name is not None:
        n["name"] = {"value": name}
    if backend is not None:
        n["backendDOMNodeId"] = backend
    if properties:
        n["properties"] = [{"name": k, "value": {"value": v}} for k, v in properties.items()]
    if value is not None:
        n["value"] = {"value": value}
    if parent is not None:
        n["parentId"] = str(parent)
    return n


def refs():
    """Like `Tab.ref_for`: one numbering across frames, and the same ref for the same element."""
    seen = {}

    def ref_for(prefix, backend):
        if (prefix, backend) not in seen:
            seen[(prefix, backend)] = f"e{len(seen) + 1}"
        return seen[(prefix, backend)]

    return ref_for


def page(*nodes, cursors=None, displays=None, children=None):
    frame = Frame("", list(nodes), cursors or {}, displays or {}, {}, children or {})
    return build(frame, refs())


def test_snapshot_hoists_wrappers_and_keeps_semantics():
    items = page(
        node(1, "RootWebArea", "Title", [2], backend=1),
        node(2, "generic", None, [3, 6, 8], backend=2, parent=1),
        node(3, "heading", "Hello", [4], backend=3, properties={"level": 2}, parent=2),
        node(4, "StaticText", "Hello", [5], parent=3),
        node(5, "InlineTextBox", "Hello", parent=4),
        node(6, "link", "Docs", [7], backend=6, properties={"url": "https://example.com/docs"}, parent=2),
        node(7, "StaticText", "Docs", parent=6),
        node(8, "none", None, [9], ignored=True, parent=2),
        node(9, "button", "Go", backend=9, properties={"disabled": True}, parent=8),
        cursors={6: "pointer", 7: "pointer"},
    )
    assert render(items) == "\n".join([
        '- heading "Hello" [level=2] [ref=e1]',
        '- link "Docs" [ref=e2]:',  # Links are clickable by nature: no [cursor=pointer].
        "  - /url: https://example.com/docs",
        '- button "Go" [disabled] [ref=e3]',
    ])


def test_snapshot_text_merging_label_dedup_and_values():
    items = page(
        node(1, "RootWebArea", "", [2, 3, 5, 7], backend=1),
        node(2, "StaticText", "Name", parent=1),
        node(3, "textbox", "Name", backend=3, value="Casper", parent=1),
        node(5, "paragraph", None, [6, 8, 9], backend=5, parent=1),
        node(6, "StaticText", "Use", parent=5),
        node(8, "strong", None, [10], backend=8, parent=5),
        node(10, "StaticText", "bold", parent=8),
        node(9, "StaticText", "words: here", parent=5),
        node(7, "generic", None, [11], backend=7, parent=1),
        node(11, "StaticText", "Drag me", parent=7),
        displays={7: "block"},
    )
    text = render(items)
    assert '- textbox "Name" [ref=e1]: Casper' in text
    assert "- text: Name" not in text  # The label repeats the field's name.
    assert "- paragraph [ref=e2]: 'Use bold words: here'" in text  # Inline formatting merged; YAML-quoted.
    assert "- generic [ref=e3]: Drag me" in text  # A block of text keeps a ref.


def test_snapshot_clickable_generic_and_iframes():
    child = Frame("f1", [node(1, "RootWebArea", "", [2], backend=1), node(2, "button", "Inner", backend=2, parent=1)])
    items = page(
        node(1, "RootWebArea", "", [2, 3], backend=1),
        node(2, "generic", None, [4], backend=2, parent=1),
        node(4, "StaticText", "Card", parent=2),
        node(3, "Iframe", "", backend=3, parent=1),
        cursors={2: "pointer"},
        children={3: child},
    )
    assert render(items) == "\n".join([
        "- generic [ref=e1] [cursor=pointer]: Card",
        "- iframe [ref=e2]:",
        '  - button "Inner" [ref=e3]',  # Refs inside frames are numbered with the page's, without a frame prefix.
    ])


def test_snapshot_depth_and_find():
    items = page(
        node(1, "RootWebArea", "", [2], backend=1),
        node(2, "navigation", "Main", [3, 4], backend=2, parent=1),
        node(3, "link", "Home", backend=3, parent=2),
        node(4, "list", None, [5], backend=4, parent=2),
        node(5, "listitem", None, [6], backend=5, parent=4),
        node(6, "link", "Pricing", backend=6, parent=5),
    )
    assert render(items, depth=1) == '- navigation "Main" [ref=e1]'
    count, shown, text = find(items, re.compile("pricing", re.I))
    assert count == shown == 1
    # A list entry that is just one link is shown as the link.
    assert text.splitlines() == ['- navigation "Main" [ref=e1]:', "  - list [ref=e3]:", '    - link "Pricing" [ref=e4]']
    assert [line.parent for line in lines(items)] == [None, 0, 0, 2]


def test_find_merges_paths_and_keeps_link_urls():
    items = page(
        node(1, "RootWebArea", "", [2, 6], backend=1, properties={"url": "https://shop.example/"}),
        node(2, "link", "SSD 1TB", [3, 4], backend=2, properties={"url": "https://shop.example/p/1"}, parent=1),
        node(3, "generic", None, [5], backend=3, parent=2),
        node(5, "StaticText", "1.810 kr.", parent=3),
        node(4, "button", "Or 563 kr./month", backend=4, parent=2),
        node(6, "link", "SSD 2TB", [7], backend=6, properties={"url": "https://shop.example/p/2"}, parent=1),
        node(7, "StaticText", "3.569 kr.", parent=6),
        displays={3: "block"},
    )
    count, shown, text = find(items, re.compile("kr"))
    assert (count, shown) == (3, 3)
    assert text.splitlines() == [
        '- link "SSD 1TB" [ref=e1]:', "  - /url: /p/1",  # The link is on the path once, with its URL.
        "  - generic [ref=e2]: 1.810 kr.",
        '  - button "Or 563 kr./month" [ref=e3]',
        '- link "SSD 2TB" [ref=e4]:', "  - text: 3.569 kr.", "  - /url: /p/2",
    ]


def test_snapshot_relative_links_and_redundant_children():
    items = page(
        node(1, "RootWebArea", "", [2, 3, 4, 5], backend=1, properties={"url": "https://shop.example/?q=ssd"}),
        node(2, "link", "Deals", backend=2, properties={"url": "https://shop.example/deals?x=1"}, parent=1),
        node(3, "link", "Skip", backend=3, properties={"url": "https://shop.example/?q=ssd#main"}, parent=1),
        node(4, "link", "Elsewhere", backend=4, properties={"url": "https://other.example/a"}, parent=1),
        node(5, "button", "Dark mode", [6], backend=5, parent=1),
        node(6, "generic", None, [7], backend=6, parent=5),
        node(7, "StaticText", "Dark mode", parent=6),
        displays={6: "block"},
    )
    assert render(items) == "\n".join([
        '- link "Deals" [ref=e1]:', "  - /url: /deals?x=1",
        '- link "Skip" [ref=e2]:', "  - /url: '#main'",
        '- link "Elsewhere" [ref=e3]:', "  - /url: https://other.example/a",
        '- button "Dark mode" [ref=e4]',
    ])


def test_keys():
    assert keys.parse("Enter")[1].keycode == 13
    modifiers, key = keys.parse("Control+Shift+a")
    assert modifiers == ["Control", "Shift"] and key.code == "KeyA"
    assert keys.parse("+")[1].key == "+"
    assert keys.parse("Control++")[0] == ["Control"]
    assert keys.parse("é")[1].text == "é"
    assert keys.parse("ControlOrMeta+c")[0] == [keys.CONTROL_OR_META]
    with pytest.raises(ValueError):
        keys.parse("Hyper+a")
    assert keys.mask(["Alt", "Shift"]) == 9


def test_is_local():
    for url in ["http://localhost:3000", "http://127.0.0.1:8080/x", "http://192.168.1.5", "https://box.tail1234.ts.net/", "file:///tmp/a.html", "http://app.localhost"]:
        assert is_local(url), url
    for url in ["https://example.com", "https://8.8.8.8/", "https://github.com/x"]:
        assert not is_local(url), url


def test_is_shell():
    assert is_shell('<html><body><div id="root"></div><script src="/app.js"></script></body></html>')
    assert is_shell('<html><body><noscript>You need to enable JavaScript to run this app.</noscript><script src=a.js></script></body></html>')
    assert not is_shell("<html><body><h1>Example Domain</h1><p>Short page.</p><script src=/s.js></script></body></html>")
    assert not is_shell("<html><body>" + "word " * 100 + "<script></script></body></html>")


def test_formatting():
    assert remote_value({"type": "string", "value": "hi"}) == "hi"
    assert remote_value({"type": "object", "preview": {"type": "object", "properties": [{"name": "a", "value": "1"}]}}) == "{a: 1}"
    assert remote_value({"type": "object", "subtype": "array", "preview": {"type": "object", "subtype": "array", "properties": [{"name": "0", "value": "1"}]}}) == "[1]"
    assert Message("error", "ERROR", "boom", "x.js:1").line() == "[ERROR] boom @ x.js:1"
    request = Request(3, "id", "s", "https://a/b", "GET", "Fetch", {}, status=200, status_text="OK")
    assert request.line() == "3. [GET] https://a/b => [200] OK"
    request.failed = "net::ERR_ABORTED"
    assert request.line() == "3. [GET] https://a/b => [FAILED] net::ERR_ABORTED"


def test_tools_cover_playwright_mcp():
    names = {tool[0] for tool in TOOLS}
    assert PLAYWRIGHT_TOOLS <= names
    assert "browse" in names
    for name, description, schema, handler, read_only, modal_ok in TOOLS:
        assert schema["type"] == "object" and set(schema["required"]) <= set(schema["properties"]), name
        assert handler == name


def test_cloudflare_bot_management_is_not_a_challenge():
    from browse_anything import cloudflare
    # Sites with Cloudflare bot management define _cf_chl_opt on every page; only the interstitial orchestrates.
    page = '<script>window._cf_chl_opt={a:1}</script><script src="/cdn-cgi/challenge-platform/scripts/precursor/main.js"></script>'
    assert not cloudflare.detect({}, page)
    assert cloudflare.detect({}, '<script>window._cf_chl_opt={a:1}</script><script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>')


def test_unwrap_search_redirects():
    import base64
    from browse_anything.snapshot import unwrap
    target = "https://www.proshop.dk/SSD/Samsung-9100/3347706"
    bing = "https://www.bing.com/ck/a?!&&p=abc&u=a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=") + "&ntb=1"
    assert unwrap(bing) == target
    assert unwrap("https://www.google.com/url?q=https://example.com/a%3Fb&sa=U") == "https://example.com/a?b"
    assert unwrap("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fx") == "https://example.org/x"
    assert unwrap("https://example.com/ck/a?u=a1xyz") == "https://example.com/ck/a?u=a1xyz"
    long = "https://example.com/" + "x" * 1000
    assert unwrap(long) == long  # URLs are never cut off.


def test_unwrap_nested_ad_redirects_and_tracking():
    import base64
    from urllib.parse import quote
    from browse_anything.snapshot import unwrap
    shop = "http://www.proshop.dk/SSD/Samsung-990-Pro/3103447?utm_source=bing&msclkid=ea75&variant=2"
    bing = "https://www.bing.com/aclick?ld=e8&u=" + base64.b64encode(quote(shop, safe="").encode()).decode()
    ddg = "https://duckduckgo.com/y.js?ad_domain=proshop.dk&u3=" + quote(bing, safe="") + "&vqd=4-1"
    assert unwrap(ddg) == "http://www.proshop.dk/SSD/Samsung-990-Pro/3103447?variant=2"  # Ad tracking dropped.
    plain = "https://shop.example/p?utm_source=newsletter"
    assert unwrap(plain) == plain  # Only redirect destinations are cleaned.


def test_missing_required_argument_is_reported():
    import asyncio
    from browse_anything.browser import Browser
    from browse_anything.session import Session
    from browse_anything.tools import Tools
    session = Session()
    tools = Tools(session, Browser(session))
    content, error = asyncio.run(tools.call("browser_type", {"text": "x", "target": None}))
    assert error and "Missing required argument: target" in content[0].text


def test_invisible_text_and_long_selects():
    countries = [f"Country {i}" for i in range(30)]
    option_nodes = [node(100 + i, "option", name, backend=100 + i, parent=4,
                         properties={"selected": True} if i == 20 else None) for i, name in enumerate(countries)]
    items = page(
        node(1, "RootWebArea", "", [2, 3], backend=1),
        node(2, "generic", None, [5], backend=2, parent=1),
        node(5, "StaticText", "\u00ad", parent=2),  # Only a soft hyphen: nothing to show.
        node(3, "combobox", "Country", [4], backend=3, parent=1),
        node(4, "MenuListPopup", None, [100 + i for i in range(30)], ignored=True, parent=3),
        *option_nodes,
        displays={2: "block"},
    )
    text = render(items)
    assert "\u00ad" not in text and not text.startswith("- generic")
    lines_ = text.splitlines()
    assert sum('- option "' in l for l in lines_) == 11  # The first ten and the selected one keep their refs.
    assert 'option "Country 20" [selected]' in text
    assert "- text: '19 more options, chosen by their text with browser_select_option: Country 10, Country 11," in text


def test_long_dialogs_are_condensed_in_excerpts():
    from browse_anything.snapshot import Item
    from browse_anything.tools import condense, visible
    legal = "We use cookies for many purposes. " * 40
    dialog = Item("dialog", "Cookies", "e1", children=[
        Item("paragraph", ref="e2", text=legal), Item("link", "Cookie notice", "e3"), Item("link", "Cookie notice", "e4"),
        Item("button", "Accept", "e5"), Item("button", "Decline", "e6")])
    text = render([condense(dialog)])
    assert len(text) < 700 and "(shortened" in text
    assert text.index('button "Accept"') < text.index('link "Cookie notice"') and text.count("Cookie notice") == 1
    assert not visible(Item("dialog", ref="e9", children=[Item("generic", ref="e10")]))
    small = Item("dialog", "Error", "e1", children=["Your card was declined.", Item("button", "OK", "e2")])
    assert condense(small) is small


def test_dialogs_condensed_anywhere_in_the_tree():
    from browse_anything.snapshot import Item
    from browse_anything.tools import condense_dialogs
    legal = "Vi bruger cookies til mange formål. " * 60
    page_ = [Item("banner", ref="e1", children=[
        Item("dialog", "Cookies", "e2", children=[Item("paragraph", ref="e3", text=legal), Item("button", "Afvis", "e4")])]),
        Item("heading", "Shop", "e5")]
    text = render(condense_dialogs(page_))
    assert "(shortened" in text and 'button "Afvis" [ref=e4]' in text and 'heading "Shop"' in text
    assert len(text) < 800


def test_idle_tabs_are_closed(monkeypatch):
    import asyncio
    from browse_anything.session import Config, Session

    class FakeTab:
        def __init__(self, used):
            self.used, self.lock = used, asyncio.Lock()

    async def run():
        session = Session(Config(tab_idle_minutes=1))
        session.cdp = type("CDP", (), {"alive": True})()
        closed = []

        async def close_tab(tab):
            closed.append(tab)
            session.tabs.remove(tab)

        session.close_tab = close_tab
        import time
        old, fresh = FakeTab(time.monotonic() - 120), FakeTab(time.monotonic())
        session.tabs = [old, fresh]
        assert await session.expire_idle() == 1 and closed == [old] and session.tabs == [fresh]
        session.config.tab_idle_minutes = 0
        old.used = time.monotonic() - 10_000
        session.tabs = [old]
        assert await session.expire_idle() == 0

    asyncio.run(run())


def test_filter_facets_collapse_to_their_controls():
    items = page(
        node(1, "RootWebArea", "", [2, 7, 8], backend=1, properties={"url": "https://shop.example/"}),
        node(2, "LabelText", None, [3], backend=2, parent=1),
        node(3, "link", "Nieuw 65", [4, 5], backend=3, properties={"url": "https://shop.example/?c=new"}, parent=2),
        node(4, "checkbox", "Nieuw 65", backend=4, properties={"checked": "true"}, parent=3),
        node(5, "StaticText", "Nieuw 65", parent=3),
        node(7, "checkbox", "Refurbished 0", backend=7, properties={"disabled": True}, parent=1),
        node(8, "StaticText", "Refurbished 0", parent=1),
        cursors={2: "pointer", 3: "pointer"},
    )
    assert render(items) == "\n".join([
        '- checkbox "Nieuw 65" [checked] [ref=e1]:',  # Label, link and repeated text are one control.
        "  - /url: /?c=new",
        '- checkbox "Refurbished 0" [disabled] [ref=e2]',  # The text after it only repeated its name.
    ])
