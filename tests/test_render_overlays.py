"""PNG-Overlays (Hook, Credit, Wasserzeichen, Emojis) – ohne echtes Netzwerk."""

from __future__ import annotations

import httpx
import numpy as np
import pytest
import respx
from PIL import Image

from wdvi.render import overlays

SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36">'
       b'<circle cx="18" cy="18" r="16" fill="#FFCC4D"/></svg>')
TWEMOJI = r"https://raw\.githubusercontent\.com/jdecked/twemoji/main/assets/svg/.*\.svg"


@pytest.fixture(autouse=True)
def _clear_negative_cache():
    overlays._failed.clear()
    yield
    overlays._failed.clear()


@pytest.fixture
def twemoji():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=TWEMOJI).mock(return_value=httpx.Response(200, content=SVG))
        yield route


def _alpha(img: Image.Image) -> np.ndarray:
    return np.asarray(img.getchannel("A"))


# --------------------------------------------------------------------------- Emoji


def test_emoji_png_downloads_rasterizes_and_caches(settings, twemoji):
    cache = settings.config.paths.cache_dir
    p = overlays.emoji_png("💀", 150, cache)
    assert p is not None and p.parent == cache / "emoji"
    img = Image.open(p)
    assert img.size == (150, 150) and img.mode == "RGBA"
    assert str(twemoji.calls[0].request.url).endswith("/1f480.svg")
    n = twemoji.call_count
    assert overlays.emoji_png("💀", 150, cache) == p  # PNG-Cache
    assert overlays.emoji_png("💀", 64, cache) is not None  # andere Größe: SVG-Cache, kein Download
    assert twemoji.call_count == n


def test_emoji_png_retries_without_fe0f(settings):
    with respx.mock(assert_all_called=False) as mock:
        full = mock.get(url__regex=r".*/2764-fe0f\.svg").mock(return_value=httpx.Response(404))
        bare = mock.get(url__regex=r".*/2764\.svg").mock(return_value=httpx.Response(200, content=SVG))
        p = overlays.emoji_png("❤️", 72, settings.config.paths.cache_dir)
    assert p is not None
    assert bare.called
    assert not full.called or full.call_count == 1


def test_emoji_png_failure_returns_none(settings):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=TWEMOJI).mock(side_effect=httpx.ConnectError("offline"))
        assert overlays.emoji_png("🔥", 72, settings.config.paths.cache_dir) is None
        assert overlays.emoji_png("", 72, settings.config.paths.cache_dir) is None


def test_split_emojis():
    parts = overlays.split_emojis("NO WAY💀🔥 BRO")
    assert parts == [("NO WAY", False), ("💀", True), ("🔥", True), (" BRO", False)]
    assert overlays.split_emojis("👨‍👩‍👧 hi") == [("👨‍👩‍👧", True), (" hi", False)]


# --------------------------------------------------------------------------- Hook / Credit / Wasserzeichen


def test_hook_png_size_alpha_and_fit(settings, twemoji, tmp_path):
    p = overlays.render_hook_png("he actually made that jump 💀", settings, tmp_path / "hook.png")
    img = Image.open(p)
    assert img.mode == "RGBA" and img.width == settings.config.render.width
    a = _alpha(img)
    assert a[0, 0] == 0 and a[-1, -1] == 0  # transparente Ecken
    x0, y0, x1, y1 = img.getchannel("A").point(lambda v: 255 if v > 200 else 0).getbbox()
    assert (x1 - x0) <= 0.9 * img.width  # Pille max ~88 % Breite
    assert 150 < img.height < 450
    # Text ist weiß/ schwarz konturiert auf Verlauf: es gibt fast weiße Pixel in der Pille
    rgb = np.asarray(img.convert("RGB"))
    assert ((rgb > 240).all(axis=2) & (a > 250)).sum() > 1000


def test_hook_png_long_text_still_fits(settings, twemoji, tmp_path):
    p = overlays.render_hook_png("this is an extremely long hook text that never ends at all", settings,
                                 tmp_path / "hook.png")
    img = Image.open(p)
    bbox = img.getchannel("A").point(lambda v: 255 if v > 200 else 0).getbbox()
    assert bbox[0] > 0 and bbox[2] < img.width


def test_hook_png_compact_is_smaller(settings, twemoji, tmp_path):
    big = Image.open(overlays.render_hook_png("he actually made that jump", settings, tmp_path / "a.png"))
    small = Image.open(overlays.render_hook_png("he actually made that jump", settings, tmp_path / "b.png",
                                                compact=True))
    assert small.height < big.height


def test_hook_png_without_network_drops_emoji(settings, tmp_path):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=TWEMOJI).mock(return_value=httpx.Response(500))
        p = overlays.render_hook_png("NO WAY 💀", settings, tmp_path / "hook.png")
    assert Image.open(p).width == settings.config.render.width


def test_credit_png(settings, twemoji, tmp_path):
    img = Image.open(overlays.render_credit_png("Twitch", "xQc", settings, tmp_path / "credit.png"))
    assert img.mode == "RGBA"
    assert 150 < img.width < 600 and 40 <= img.height <= 80
    a = _alpha(img)
    assert a[0, 0] < 40  # abgerundete Ecke
    assert 120 < a[img.height // 2, img.width - 12] < 255  # halbtransparente Pille
    assert twemoji.called  # Kamera-Emoji als Bild


def test_credit_png_long_name_and_fallback_icon(settings, tmp_path):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=TWEMOJI).mock(side_effect=httpx.ConnectError("offline"))
        img = Image.open(overlays.render_credit_png("Reddit", "u/" + "x" * 80 + "中文", settings,
                                                    tmp_path / "credit.png"))
    assert img.width < 0.7 * settings.config.render.width


def test_watermark_png(settings, tmp_path):
    img = Image.open(overlays.render_watermark_png(settings, tmp_path / "wm.png"))
    assert img.mode == "RGBA" and img.width < 400 and img.height < 80
    a = _alpha(img)
    assert 0 < a.max() < 255  # halbtransparent
    rgb = np.asarray(img.convert("RGB")).astype(int)
    red = (rgb[:, :, 0] > 180) & (rgb[:, :, 1] < 60) & (a > 100)
    assert red.sum() > 50  # "WASTED" in color_wasted
