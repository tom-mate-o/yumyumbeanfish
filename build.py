#!/usr/bin/env python3
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import markdown
import yaml
from jinja2 import Environment, FileSystemLoader
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
CONTENT_DIR = ROOT / "content"
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "_site"

BASE_URL = os.environ.get("SITE_BASEURL", "").rstrip("/")
LOCAL_TZ = ZoneInfo("Europe/Vienna")

MD = markdown.Markdown(extensions=["extra", "sane_lists"])


def format_updated(iso_string):
    if not iso_string:
        return ""
    try:
        dt = datetime.fromisoformat(iso_string)
    except ValueError:
        return iso_string

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    local_dt = dt.astimezone(LOCAL_TZ)
    return local_dt.strftime("%d %b %Y, %H:%M %Z")


def coerce_updated_str(value):
    if isinstance(value, str):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def load_json_data(name, default):
    import json

    path = DATA_DIR / name
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_series():
    path = CONTENT_DIR / "series.yaml"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def parse_frontmatter(text):
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    _, fm_raw, body = parts
    meta = yaml.safe_load(fm_raw) or {}
    return meta, body.strip()


def output_path_for(md_path: Path) -> Path:
    stem = md_path.stem
    if stem == "index":
        return OUT_DIR / "index.html"
    return OUT_DIR / stem / "index.html"


def nav_active_for(md_path: Path) -> str:
    return md_path.stem


def render_content_pages(env, base_context):
    for md_path in sorted(CONTENT_DIR.glob("*.md")):
        raw = md_path.read_text(encoding="utf-8")
        meta, body_md = parse_frontmatter(raw)

        MD.reset()
        content_html = MD.convert(body_md)

        layout = meta.get("layout", "page.html")
        template = env.get_template(layout)

        context = dict(base_context)
        context.update(meta)
        context["title"] = meta.get("title", md_path.stem.capitalize())
        context["content_html"] = content_html
        context["nav_active"] = nav_active_for(md_path)

        page_updated_iso = ""
        raw_updated = meta.get("updated")
        if raw_updated is not None:
            page_updated_iso = coerce_updated_str(raw_updated)
        elif md_path.stem == "now":
            mtime = datetime.fromtimestamp(md_path.stat().st_mtime, tz=timezone.utc)
            page_updated_iso = mtime.isoformat()
        context["updated_display"] = format_updated(page_updated_iso)
        context["updated_iso"] = page_updated_iso

        html = template.render(**context)

        out_path = output_path_for(md_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        print(f"gebaut: {md_path.name} -> {out_path.relative_to(ROOT)}")


def render_data_page(env, template_name, out_rel, nav_active, title, base_context):
    template = env.get_template(template_name)
    context = dict(base_context)
    context["title"] = title
    context["nav_active"] = nav_active

    html = template.render(**context)

    out_path = OUT_DIR / out_rel / "index.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"gebaut: {template_name} -> {out_path.relative_to(ROOT)}")


def _retry(func, *args, attempts=6, delay=0.25, **kwargs):
    last_exc = None
    for _ in range(attempts):
        try:
            return func(*args, **kwargs)
        except PermissionError as exc:
            last_exc = exc
            time.sleep(delay)
    raise last_exc


def _sync_dir(src: Path, dest: Path):
    _retry(dest.mkdir, parents=True, exist_ok=True)
    kept = set()

    for root, _dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        dest_root = dest / rel_root
        _retry(dest_root.mkdir, parents=True, exist_ok=True)
        for name in files:
            src_file = Path(root) / name
            dest_file = dest_root / name
            kept.add(str(rel_root / name))

            if dest_file.exists():
                try:
                    same_size = dest_file.stat().st_size == src_file.stat().st_size
                    newer = dest_file.stat().st_mtime >= src_file.stat().st_mtime
                    if same_size and newer:
                        continue
                except OSError:
                    pass

            try:
                _retry(shutil.copy2, src_file, dest_file)
            except PermissionError as exc:
                print(f"::warning::Konnte {dest_file} nicht schreiben ({exc}), ueberspringe", file=sys.stderr)

    for root, _dirs, files in os.walk(dest):
        rel_root = Path(root).relative_to(dest)
        for name in files:
            rel = str(rel_root / name)
            if rel not in kept:
                try:
                    os.remove(Path(root) / name)
                except OSError:
                    pass


def copy_static():
    dest = OUT_DIR / "static"
    _sync_dir(STATIC_DIR, dest)
    print(f"kopiert: static/ -> {dest.relative_to(ROOT)}")


def build(clean=False):
    if clean and OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    photos_data = load_json_data("photos.json", {"updated_at": None, "photos": []})
    records_data = load_json_data("records.json", {"updated_at": None, "records": []})

    photos_updated_iso = photos_data.get("updated_at") or ""
    records_updated_iso = records_data.get("updated_at") or ""

    base_context = {
        "base_url": BASE_URL,
        "build_year": datetime.now(timezone.utc).year,
        "photos": photos_data.get("photos", []),
        "featured": photos_data.get("featured"),
        "records": records_data.get("records", []),
        "series": load_series(),
        "photos_updated_display": format_updated(photos_updated_iso),
        "photos_updated_iso": photos_updated_iso,
        "records_updated_display": format_updated(records_updated_iso),
        "records_updated_iso": records_updated_iso,
        "web3forms_key": os.environ.get("WEB3FORMS_ACCESS_KEY", "YOUR_WEB3FORMS_ACCESS_KEY"),
    }

    render_content_pages(env, base_context)
    render_data_page(env, "gallery.html", "gallery", "gallery", "Gallery", base_context)
    render_data_page(env, "records.html", "records", "records", "Records", base_context)
    render_data_page(env, "contact.html", "contact", "contact", "Contact", base_context)

    copy_static()

    (OUT_DIR / ".nojekyll").touch()

    print(f"Fertig. Seite liegt in {OUT_DIR.relative_to(ROOT)}/")


def source_files():
    for base in (CONTENT_DIR, TEMPLATES_DIR, STATIC_DIR):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                yield path
    for name in ("photos.json", "records.json"):
        path = DATA_DIR / name
        if path.exists():
            yield path


def snapshot():
    return {str(p): p.stat().st_mtime for p in source_files()}


def watch(clean=False):
    build(clean=clean)
    print("\nWatch-Modus aktiv - Änderungen an content/, templates/, static/ oder data/*.json bauen automatisch neu.")
    print("Zum Beenden: Strg+C\n")
    last = snapshot()
    try:
        while True:
            time.sleep(1)
            current = snapshot()
            if current != last:
                print("Änderung erkannt, baue neu ...")
                try:
                    build()
                except Exception as exc:
                    print(f"Build-Fehler: {exc}", file=sys.stderr)
                last = current
    except KeyboardInterrupt:
        print("\nWatch-Modus beendet.")


def main():
    clean = "--clean" in sys.argv
    if "--watch" in sys.argv:
        watch(clean=clean)
    else:
        build(clean=clean)


if __name__ == "__main__":
    main()
