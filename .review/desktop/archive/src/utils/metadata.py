"""Определение даты файла: EXIF → дата в имени → время изменения."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from ..config import PHOTO_EXTENSIONS

try:
    import exifread
    # exifread пишет «File format not recognized» в консоль на каждой битой картинке;
    # для нас это не ошибка — просто переходим к дате из имени файла / mtime.
    logging.getLogger("exifread").setLevel(logging.CRITICAL)
except ImportError:  # без exifread работаем по имени файла и mtime
    exifread = None

MIN_YEAR = 1990

# 14 цифр подряд: YYYYMMDDHHMMSS
_RE_DATETIME14 = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?!\d)")
# YYYYMMDD или YYYY-MM-DD / YYYY_MM_DD / YYYY.MM.DD (IMG_20240315_..., IMG-20240315-WA0001)
_RE_DATE = re.compile(r"(?<!\d)(\d{4})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)")
# unix-timestamp ровно из 10 (секунды) или 13 (миллисекунды) цифр
_RE_TIMESTAMP = re.compile(r"(?<!\d)(\d{13}|\d{10})(?!\d)")


def _year_ok(year: int) -> bool:
    return MIN_YEAR <= year <= datetime.now().year + 1


def _build(y, m, d, hh=0, mm=0, ss=0) -> datetime | None:
    try:
        dt = datetime(int(y), int(m), int(d), int(hh), int(mm), int(ss))
    except ValueError:
        return None
    return dt if _year_ok(dt.year) else None


def _get_date_from_exif(path: Path, audit=None) -> datetime | None:
    if exifread is None or path.suffix.lower() not in PHOTO_EXTENSIONS:
        return None
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False, stop_tag="DateTimeOriginal")
        for key in ("EXIF DateTimeOriginal", "Image DateTime"):
            if key in tags:
                dt = datetime.strptime(str(tags[key]).strip(), "%Y:%m:%d %H:%M:%S")
                if _year_ok(dt.year):
                    return dt
    except Exception as exc:
        if audit:
            audit.exception(exc, operation='read_exif', path=str(path), fallback='filename_then_mtime')
    return None


def _get_date_from_filename(path: Path) -> datetime | None:
    stem = path.stem

    for m in _RE_DATETIME14.finditer(stem):
        dt = _build(*m.groups())
        if dt:
            return dt

    for m in _RE_DATE.finditer(stem):
        dt = _build(*m.groups())
        if dt:
            return dt

    for m in _RE_TIMESTAMP.finditer(stem):
        raw = int(m.group(1))
        if len(m.group(1)) == 13:
            raw //= 1000
        try:
            dt = datetime.fromtimestamp(raw)
        except (OSError, OverflowError, ValueError):
            continue
        if _year_ok(dt.year) and dt.year >= 2000:
            return dt
    return None


def resolve_date(path: Path, audit=None) -> datetime:
    """Всегда возвращает datetime."""
    for source, resolver in (('exif', lambda: _get_date_from_exif(path, audit)),
                             ('filename', lambda: _get_date_from_filename(path)),
                             ('mtime', lambda: datetime.fromtimestamp(path.stat().st_mtime))):
        value = resolver()
        if audit:
            reason = ('selected' if value else
                      'exif_library_unavailable' if source == 'exif' and exifread is None else
                      'unsupported_exif_format' if source == 'exif' and path.suffix.lower() not in PHOTO_EXTENSIONS else
                      'no_valid_date')
            audit.event('date_candidate', path=str(path), source=source,
                        value=value.isoformat() if value else None,
                        reason=reason,
                        exif_available=exifread is not None)
        if value:
            return value
