"""Настройки проекта: справочники расширений, имена папок, сохранение настроек."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

DATA_DIR = Path.home() / ".archivarius"
SETTINGS_FILE = DATA_DIR / "settings.json"

# --- Имена папок внутри «Назначения» ---
MEDIA_DIR = "Media_Incoming"
DOCS_DIR = "Documents_Sorted"
AUDIO_DIR = "Audio_Sorted"
OTHER_DIR = "Other_Unsorted"
PROCESSED_DIR = "_Processed_Archives"   # сюда уходят распакованные архивы
JUNK_DIR = "_Archivarius_junk"          # служебные файлы с возможностью отката
DUPLICATES_DIR = "_Duplicate_Files"
LOGS_DIR = "_Archivarius_logs"          # журналы перемещений и логи запусков

EXTRACTED_SUFFIX = "_extracted"
EXTRACTION_STATE_FILE = ".archivarius-extraction.json"
MAX_NESTING = 3  # на сколько уровней вглубь распаковывать архивы в архивах

PHOTO_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".heic", ".heif", ".raw", ".cr2", ".cr3", ".nef",
    ".arw", ".dng", ".orf", ".rw2", ".pef", ".srw",
}

# ВНИМАНИЕ: ".ts" намеренно убран. Это и видео-поток, и TypeScript-файлы;
# исходники из архивов не должны попадать в медиатеку.
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm",
    ".m4v", ".3gp", ".3g2", ".mts", ".m2ts", ".mpg", ".mpeg",
}

MEDIA_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS

# Документы раскладываются по подпапкам: Documents_Sorted/<ключ>/
DOC_FOLDERS = {
    "PDF": {".pdf"},
    "Word": {".doc", ".docx", ".odt", ".rtf"},
    "Excel": {".xls", ".xlsx", ".ods", ".csv"},
    "Presentations": {".ppt", ".pptx", ".odp"},
    "Text": {".txt"},
    "Books": {".epub", ".fb2", ".djvu"},
}
DOC_BY_EXT = {ext: folder for folder, exts in DOC_FOLDERS.items() for ext in exts}
DOC_EXTENSIONS = set(DOC_BY_EXT)

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a", ".wma",
    ".opus", ".aiff", ".ape",
}

ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}

# Служебные файлы: сохраняются отдельно при сортировке
JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}


def get_7z_path() -> str | None:
    """Ищет 7-Zip: сначала в PATH, затем в стандартных папках Windows."""
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return found

    if os.name == "nt":
        roots = [
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            r"C:\Program Files",
            r"C:\Program Files (x86)",
        ]
        for root in filter(None, roots):
            candidate = Path(root) / "7-Zip" / "7z.exe"
            if candidate.exists():
                return str(candidate)
    return None


DEFAULT_SETTINGS = {
    "src": "",
    "tgt": "",
    "quar": "",
    "media": True,
    "docs": True,
    "audio": True,
    "other": True,
    "archive_action": "move",   # "move" | "delete"
    "dry_run": True,            # первый запуск всегда безопасный
}


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
