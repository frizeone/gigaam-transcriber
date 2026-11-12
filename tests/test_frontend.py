"""Проверки статического фронтенда: без CDN и сборщиков."""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_static_files_exist() -> None:
    for name in ("index.html", "app.css", "app.js", "favicon.svg"):
        assert (STATIC / name).is_file()
        assert (STATIC / name).stat().st_size > 0


def test_no_external_resources() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    combined = html + css + js
    assert "https://" not in combined
    assert "http://" not in combined
    assert "cdn." not in combined
    assert "/app.css" in html
    assert "/app.js" in html


def test_accessible_labels() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'lang="ru"' in html
    assert "aria-live" in html
    assert "role=\"progressbar\"" in html
    assert 'aria-label="Выберите файл' in html
