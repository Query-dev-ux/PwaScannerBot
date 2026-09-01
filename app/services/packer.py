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
    real = [p for p in plist if (p.get("service") or "") != "stage"]
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
    return str(zip_path)
