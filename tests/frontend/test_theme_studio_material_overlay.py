"""Check the material and palette cascade using the shipped loader and stylesheet."""
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from theme_studio.materials import compile_materials, default_materials, material_attributes


ASSETS = Path(__file__).resolve().parents[2] / "static/theme-studio/loader"
HTML = """<!doctype html><body>
<div id="react-chat-window-shell"><div id="react-chat-window-root">
  <div class="compact-chat-surface-frame">
    <div class="window-topbar"></div><div class="composer-input-shell"></div>
    <button id="sendButton">Send</button>
  </div>
  <div class="message-list"><div class="message-bubble-user"></div>
    <div class="message-bubble-assistant"></div></div>
  <div class="compact-export-history-message is-user"><div class="compact-export-history-bubble"></div></div>
</div></div></body>"""

PALETTE = {
    "--nts-user-bubble-bg": "rgba(19, 52, 86, 0.65)",
    "--nts-ai-bubble-bg": "#685634",
    "--nts-topbar-bg": "#234567",
    "--nts-input-bg": "#453667",
    "--nts-send-bg": "#725634",
    "--nts-chat-bg": "#345678",
    "--nts-user-bubble-gradient": "linear-gradient(27deg, #345678, #876543)",
    "--nts-ai-bubble-gradient": "linear-gradient(137deg, #246810, #102846)",
    "--nts-chat-gradient": "linear-gradient(58deg, #123456, #654321)",
    "--nts-panel-gradient": "linear-gradient(65deg, #302010, #203040)",
}


def style(page, selector, prop):
    return page.locator(selector).evaluate("(el, prop) => getComputedStyle(el)[prop]", prop)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_material_overlays_saved_palette(page: Page, mode):
    theme = {
        "schema": 1, "name": "overlay", "enabled": True, "vars": PALETTE,
        "dark_vars": {**PALETTE, "--nts-user-bubble-bg": "rgba(93, 64, 37, 0.45)"},
    }
    page.route("http://theme.test/**", lambda route: route.fulfill(status=404))
    page.route("http://theme.test/chat", lambda route: route.fulfill(content_type="text/html", body=HTML))
    page.route("**/api/theme_studio/theme.json*", lambda route: route.fulfill(json=theme))
    page.goto("http://theme.test/chat")
    page.locator("html").evaluate("(el, mode) => el.dataset.theme = mode", mode)
    page.add_style_tag(path=ASSETS / "neko-theme-studio.css")
    page.add_script_tag(path=ASSETS / "neko-theme-studio.js")
    user_color = PALETTE["--nts-user-bubble-bg"] if mode == "light" else theme["dark_vars"]["--nts-user-bubble-bg"]
    expect(page.locator(".message-bubble-user")).to_have_css("background-color", user_color)

    roles = {
        ".message-bubble-user": "bubble", ".message-bubble-assistant": "bubble",
        ".compact-export-history-bubble": "bubble", ".window-topbar": "topbar",
        ".composer-input-shell": "input", "#sendButton": "button",
        ".compact-chat-surface-frame": "chat", ".message-list": "sidebar",
    }
    original_colors = {el: style(page, el, "backgroundColor") for el in roles}
    original_images = {el: style(page, el, "backgroundImage") for el in roles}
    for el, angle in {
        ".message-bubble-user": 27, ".compact-export-history-bubble": 27,
        ".message-bubble-assistant": 137, ".message-list": 58, ".compact-chat-surface-frame": 65,
    }.items():
        assert f"linear-gradient({angle}deg" in original_images[el], el
    material = default_materials()
    material["enabled"] = True
    material["global"].update(tint="#ab354d", opacity=0.6)
    material["components"] = {
        "bubble": {"mode": "custom", "opacity": 0.25},
        "topbar": {"mode": "custom", "opacity": 0.8},
        "button": {"mode": "custom", "opacity": 0.45},
        "chat": {"mode": "custom", "opacity": 0.15},
    }
    for strength in (0.4, 0):
        material["global"]["tintStrength"] = strength
        light, dark = compile_materials(material)
        theme.update(material_vars={"light": light, "dark": dark}, material_attrs=material_attributes(material))
        page.evaluate("theme => NekoThemeStudio.apply(theme)", theme)
        compiled = theme["material_vars"][mode]
        for el, role in roles.items():
            assert style(page, el, "backgroundColor") == original_colors[el], el
            tint = compiled.get(f"--nts-mt-c-bg-{role}", compiled["--nts-mt-bg"])
            film = page.evaluate("""tint => {
                const s = document.createElement('span').style;
                s.backgroundImage = 'linear-gradient(' + tint + ', ' + tint + ')';
                return s.backgroundImage;
            }""", tint)
            image = style(page, el, "backgroundImage")
            assert film in image, el
            if original_images[el] != "none":
                assert image.endswith(original_images[el]), el
        assert "radial-gradient" in style(page, ".message-bubble-user", "backgroundImage")
        assert "blur(" in style(page, ".message-bubble-user", "backdropFilter")
        assert style(page, ".compact-export-history-bubble", "backdropFilter") == "none"
        assert style(page, "#react-chat-window-root", "backgroundColor") == "rgb(52, 86, 120)"

    page.locator("body").evaluate("el => el.classList.add('subtitle-web-host')")
    page.locator("#react-chat-window-shell").evaluate("el => el.dataset.chatSurfaceMode = 'compact'")
    expect(page.locator("#react-chat-window-root")).to_have_css("background-color", "rgba(0, 0, 0, 0)")
    theme.pop("material_vars")
    theme.pop("material_attrs")
    page.evaluate("theme => NekoThemeStudio.apply(theme)", theme)
    for el in roles:
        assert style(page, el, "backgroundImage") == original_images[el], el
        assert style(page, el, "backgroundColor") == original_colors[el], el
