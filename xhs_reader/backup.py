"""Portable history backups. Imports are validated, additive, and never overwrite data."""
import json
import math
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from . import agent, store

MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_FILES = 20000
MANIFEST = {"format": "xhs-history", "version": 1}


def _idle():
    if any(agent.is_running(cid) for cid in list(agent._runs)):
        raise RuntimeError("请先等待回答结束或停止任务，再备份或导入")


def export_to(dest):
    with agent._lock:
        _idle()
        size = count = 0
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(MANIFEST))
            for prefix, root in (("chats", agent.CHATS_DIR), ("research", store.RESEARCH_DIR)):
                for f in sorted(root.rglob("*")):
                    if not f.is_file() or f.is_symlink() or any((root / p).is_symlink() for p in f.relative_to(root).parents):
                        continue
                    name = prefix + "/" + f.relative_to(root).as_posix()
                    if not _allowed(name):
                        continue
                    size += f.stat().st_size
                    count += 1
                    if size > MAX_EXPANDED or count > MAX_FILES:
                        raise ValueError("历史数据超过备份上限（1 GB 或 20000 个文件）")
                    z.write(f, name)
        if Path(dest).stat().st_size > MAX_ARCHIVE:
            raise ValueError("备份压缩后超过 256 MB，暂时无法导入")


def _allowed(name):
    p = PurePosixPath(name)
    if "\\" in name or p.as_posix() != name or p.is_absolute() or any(x in (".", "..") for x in name.split("/")):
        return False
    parts = p.parts
    safe_id = lambda s: bool(re.fullmatch(r"[\w-]+", s))
    if name == "manifest.json":
        return True
    if len(parts) == 2 and parts[0] == "chats" and parts[1].endswith(".json"):
        return safe_id(parts[1][:-5])
    if len(parts) == 3 and parts[0] == "chats" and safe_id(parts[1]):
        return bool(re.fullmatch(r"[\w-]+\.(png|jpg|jpeg|webp|gif)", parts[2]))
    if len(parts) in (4, 5) and parts[:2] == ("chats", ".trash") and safe_id(parts[2]):
        return (len(parts) == 4 and parts[3] == "record.json") or (
            len(parts) == 5 and parts[3] == "images" and bool(re.fullmatch(r"[\w-]+\.(png|jpg|jpeg|webp|gif)", parts[4])))
    if len(parts) == 3 and parts[0] == "research" and safe_id(parts[1]):
        return parts[2] in ("meta.json", "notes.json", "report.md")
    if len(parts) == 5 and parts[0] == "research" and safe_id(parts[1]) and parts[2] == "images" and safe_id(parts[3]):
        return bool(re.fullmatch(r"\d+\.(png|jpg|jpeg|webp|gif)", parts[4]))
    return False


def import_from(source, preview=False):
    with agent._lock:
        _idle()
    if Path(source).stat().st_size > MAX_ARCHIVE:
        raise ValueError("备份文件不能超过 256 MB")
    # Stage beside the destination so committed moves remain on the same filesystem.
    agent.CHATS_DIR.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="history-import-", dir=agent.CHATS_DIR.parent) as tmp:
        stage = Path(tmp)
        try:
            with zipfile.ZipFile(source) as z:
                entries = z.infolist()
                if len(entries) > MAX_FILES + 1 or sum(i.file_size for i in entries) > MAX_EXPANDED:
                    raise ValueError("备份解压后超过上限")
                names = set()
                for i in entries:
                    if i.filename in names or not _allowed(i.filename) or stat.S_ISLNK(i.external_attr >> 16) or i.flag_bits & 1:
                        raise ValueError("备份包含不支持的路径、重复文件或加密文件")
                    names.add(i.filename)
                if json.loads(z.read("manifest.json")) != MANIFEST:
                    raise ValueError("不支持的备份格式")
                for i in entries:
                    f = stage / i.filename
                    f.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(i) as src, f.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        except (zipfile.BadZipFile, KeyError, UnicodeError) as e:
            raise ValueError("备份文件损坏或格式不正确") from e

        # Validate all records before moving any file into the live data directories.
        records = list((stage / "chats").glob("*.json")) + list((stage / "chats" / ".trash").glob("*/record.json"))
        for f in records:
            cid = f.parent.name if f.name == "record.json" else f.stem
            c = agent.validate_chat(json.loads(f.read_text(encoding="utf-8")), cid)
            if not isinstance(c.get("title"), str):
                raise ValueError("备份中的对话标题无效")
            for key in ("created", "updated"):
                if c.get(key) is not None and not isinstance(c[key], str):
                    raise ValueError("备份中的日期格式无效")
            if c.get("backend", "claude") not in ("api", "claude", "codex") or not isinstance(c.get("model_config", {}), dict):
                raise ValueError("备份中的模型配置无效")
            api = c.get("model_config", {}).get("api", {})
            if not isinstance(api, dict):
                raise ValueError("备份中的模型配置无效")
            api.pop("api_key", None)
            # Imported CLI sessions belong to another installation. Follow-ups start fresh.
            for key in ("claude_session", "codex_thread", "llm_history"):
                c.pop(key, None)
            for q in c.pop("queued", []):
                c["messages"].append({"role": "user", "text": q.get("text", ""), "images": q.get("images", []), "recovered_queue": True})
            for m in c["messages"]:
                m.pop("draft_html", None)
                if not isinstance(m.get("text", ""), str) or not isinstance(m.get("draft", ""), str):
                    raise ValueError("备份中的消息格式无效")
                if any(not isinstance(p.get("text", ""), str) for p in m.get("parts", [])):
                    raise ValueError("备份中的回答格式无效")
                for key in ("started", "elapsed", "last_activity"):
                    if key in m and (not isinstance(m[key], (int, float)) or not math.isfinite(m[key])):
                        raise ValueError("备份中的耗时格式无效")
                if "usage" in m and (not isinstance(m["usage"], dict) or any(
                        not isinstance(v, (int, float)) or not math.isfinite(v) for v in m["usage"].values())):
                    raise ValueError("备份中的用量格式无效")
                if not isinstance(m.get("images", []), list):
                    raise ValueError("备份中的图片列表无效")
                for name in m.get("images", []):
                    if not isinstance(name, str) or not re.fullmatch(r"[\w-]+\.(png|jpg|jpeg|webp|gif)", name):
                        raise ValueError("备份中的图片路径无效")
                if m.get("status") == "running":
                    m["status"] = "stopped"
                for p in m.get("parts", []):
                    p.pop("html", None)
                    for key in ("error", "query", "keyword", "title", "session", "command"):
                        if key in p and not isinstance(p[key], str):
                            raise ValueError("备份中的步骤格式无效")
                    for key in ("notes", "links"):
                        if key in p and not isinstance(p[key], list):
                            raise ValueError("备份中的步骤列表无效")
                    if any(not isinstance(link, dict) or not isinstance(link.get("url", ""), str) for link in p.get("links", [])):
                        raise ValueError("备份中的网页链接无效")
                    if p.get("status") == "running":
                        p["status"] = "error"
            c["llm_history"] = [
                {"role": m["role"], "content": m.get("text", "") if m["role"] == "user" else
                 "\n".join([*(p.get("text", "") for p in m.get("parts", []) if p["type"] == "text"), m.get("draft", "")])}
                for m in c["messages"]]
            f.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        for f in (stage / "research").glob("*/*.json"):
            value = json.loads(f.read_text(encoding="utf-8"))
            if f.name == "meta.json" and (not isinstance(value, dict) or value.get("id") != f.parent.name):
                raise ValueError("备份中的笔记元数据无效")
            if f.name == "notes.json" and (not isinstance(value, list) or any(not isinstance(n, dict) for n in value)):
                raise ValueError("备份中的笔记格式无效")
        result = {"imported": 0, "skipped": 0, "research": 0, "trash": 0}
        moved = []
        with agent._lock:
            _idle()
            plan, seen, duplicates = [], set(), []
            for f in records:
                trashed = f.name == "record.json"
                cid = f.parent.name if trashed else f.stem
                target = agent.CHATS_DIR / ".trash" / cid if trashed else agent._path(cid)
                if cid in seen or agent._path(cid).exists() or agent.image_dir(cid).exists() or (agent.CHATS_DIR / ".trash" / cid).exists():
                    result["skipped"] += 1
                    if len(duplicates) < 10:
                        duplicates.append(json.loads(f.read_text(encoding="utf-8"))["title"])
                    continue
                seen.add(cid)
                if not trashed and f.with_suffix("").exists():
                    plan.append((f.with_suffix(""), agent.image_dir(cid)))
                plan.append((f.parent if trashed else f, target))
                result["trash" if trashed else "imported"] += 1
            for folder in (stage / "research").glob("*"):
                if not (store.RESEARCH_DIR / folder.name).exists():
                    plan.append((folder, store.RESEARCH_DIR / folder.name))
                    result["research"] += 1
            if preview:
                return {**result, "conversations": len(records), "duplicates": duplicates,
                        "attachments": sum(PurePosixPath(i.filename).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif") for i in entries),
                        "expanded_bytes": sum(i.file_size for i in entries)}
            try:
                for source_path, target in plan:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source_path.rename(target)
                    moved.append(target)
            except Exception:
                for target in reversed(moved):
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                raise
        return result
