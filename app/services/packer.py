from __future__ import annotations

import json
import time
import zipfile
from datetime import datetime
from pathlib import Path

_STAGE_ORDER = ["install", "registration", "deposit", None]
_STAGE_TITLE = {
    "install": "ПОСЛЕ УСТАНОВКИ",
    "registration": "ПОСЛЕ РЕГИСТРАЦИИ",
    "deposit": "ПОСЛЕ ДЕПОЗИТА",
    None: "БЕЗ СТАДИИ",
}


def _row_get(row, key):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def build_pack(row, pushes, sessions_dir: str) -> str:
    """Write pushes.json + pushes.txt for a session and zip them. Returns zip path."""
    out_dir = Path(sessions_dir) / row["id"]
    out_dir.mkdir(parents=True, exist_ok=True)

    plist = [dict(p) for p in pushes]
    # backfill title/body/icon/image/url from raw (rows stored before a parser
    # change, and `image` which older rows never had a column for)
    for p in plist:
        if (p.get("service") or "") == "stage":
            continue
        if p.get("raw"):
            try:
                from app.utils import extract_push_fields

                f = extract_push_fields(json.loads(p["raw"]))
                for k, v in f.items():
                    if v and not p.get(k):
                        p[k] = v
            except Exception:
                pass
    real = [p for p in plist if (p.get("service") or "") != "stage"]

    # pull the notification banner images into the archive
    img_dir = out_dir / "images"
    img_map: dict = {}
    for p in real:
        src = p.get("image")
        if not src or src in img_map:
            if src:
                p["image_file"] = img_map[src]
            continue
        try:
            import requests

            r = requests.get(src, timeout=20)
            if r.ok and r.content:
                img_dir.mkdir(parents=True, exist_ok=True)
                ext = ".jpg"
                ct = r.headers.get("content-type", "")
                if "png" in ct:
                    ext = ".png"
                elif "webp" in ct:
                    ext = ".webp"
                name = f"images/{len(img_map) + 1:02d}{ext}"
                (out_dir / name).write_bytes(r.content)
                img_map[src] = name
                p["image_file"] = name
        except Exception:
            pass
    deep_link = _row_get(row, "deep_link")

    by_stage: dict = {}
    for p in plist:
        by_stage.setdefault(p.get("stage"), []).append(p)

    payload = {
        "session": row["id"],
        "pwa_name": row["pwa_name"],
        "site_url": row["site_url"],
        "start_url": row["start_url"],
        "deep_link": deep_link,
        "collected_from": row["created_at"],
        "collected_to": time.time(),
        "count": len(real),
        "count_by_stage": {
            (k or "none"): len([p for p in v if (p.get("service") or "") != "stage"])
            for k, v in by_stage.items()
        },
        "pushes": plist,
    }
    json_path = out_dir / "pushes.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"PWA:    {row['pwa_name']}",
        f"Сайт:   {row['site_url']}",
        f"start:  {row['start_url']}",
        f"in-app: {deep_link or '-'}",
        f"Пушей:  {len(real)}",
        "=" * 48,
    ]
    for stage in _STAGE_ORDER:
        group = by_stage.get(stage)
        if not group:
            continue
        n = len([p for p in group if (p.get("service") or "") != "stage"])
        lines += ["", f"### {_STAGE_TITLE.get(stage, str(stage))}  ({n})", ""]
        for p in group:
            dt = datetime.fromtimestamp(p["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            if (p.get("service") or "") == "stage":
                lines.append(f"[{dt}]  {p.get('title') or ''}")
                continue
            lines.append(f"[{dt}]  {p.get('service') or ''} / {p.get('event') or ''}")
            if p.get("title"):
                lines.append(f"  {p['title']}")
            if p.get("body"):
                lines.append(f"  {p['body']}")
            if p.get("image_file"):
                lines.append(f"  🖼 {p['image_file']}")
            elif p.get("image"):
                lines.append(f"  🖼 {p['image']}")
            if p.get("url"):
                lines.append(f"  -> {p['url']}")
            if not (p.get("title") or p.get("body")) and p.get("raw"):
                lines.append(f"  raw: {p['raw']}")
            lines.append("")
    txt_path = out_dir / "pushes.txt"
    txt_path.write_text("\n".join(lines), encoding="utf-8")

    zip_path = out_dir / f"pushes_{row['id'][:8]}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(json_path, "pushes.json")
        z.write(txt_path, "pushes.txt")
        for src, name in img_map.items():
            fp = out_dir / name
            if fp.exists():
                z.write(fp, name)
    return str(zip_path)
