import io

from wdvi import cli


def test_utf8_console_avoids_unicode_crash(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(cli.sys, "stdout", stream)
    monkeypatch.setattr(cli.sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    cli._utf8_console()
    print("✅ ok", file=stream)
    stream.flush()
    assert "✅".encode() in raw.getvalue()
