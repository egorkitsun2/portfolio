"""Сортировка файлов по категориям."""
from __future__ import annotations

import re
import os
from pathlib import Path

from ..config import (AUDIO_DIR, AUDIO_EXTENSIONS, DOC_BY_EXT, DOC_EXTENSIONS, DOCS_DIR,
                      JUNK_DIR, JUNK_NAMES, MEDIA_DIR, MEDIA_EXTENSIONS, OTHER_DIR, EXTRACTION_STATE_FILE)
from ..utils.file_ops import cleanup_empty_dirs, move_file
from ..utils.metadata import resolve_date
from ..utils.reporting import ReportCancelled, ReportWriteError, capture_file
from .context import Context
from .unpacker import is_archive_like, source_files, incomplete_extraction_reason


def _is_junk(path: Path) -> bool:
    name = path.name.lower()
    return name in JUNK_NAMES or name.startswith("._")  # ._* — мусор macOS


def _plan(ctx: Context, f: Path):
    """Возвращает (метка, папка назначения, причина пропуска)."""
    o, tgt = ctx.options, ctx.tgt
    ext = f.suffix.lower()

    if ext in MEDIA_EXTENSIONS:
        if not o.media:
            return None, None, "Категория «Медиа» отключена"
        d = resolve_date(f, ctx.audit)
        return "МЕДИА", tgt / MEDIA_DIR / f"{d:%Y}" / f"{d:%Y-%m}", ""

    if ext in DOC_EXTENSIONS:
        if not o.docs:
            return None, None, "Категория «Документы» отключена"
        return "ДОКУМЕНТ", tgt / DOCS_DIR / DOC_BY_EXT[ext], ""

    if ext in AUDIO_EXTENSIONS:
        if not o.audio:
            return None, None, "Категория «Аудио» отключена"
        return "АУДИО", tgt / AUDIO_DIR, ""

    if o.other:
        sub = re.sub(r"\W", "_", ext.lstrip("."))[:20].lower() or "no_extension"
        return "ПРОЧЕЕ", tgt / OTHER_DIR / sub, ""
    return None, None, "Тип не распознан"


def _handle(ctx: Context, f: Path, stats: dict) -> str:
    if _is_junk(f):
        folder = ctx.tgt / JUNK_DIR
        if ctx.audit:
            ctx.audit.event('file_decision', path=str(f), category='junk', destination=str(folder), reason='service_filename', extension=f.suffix.lower())
            ctx.audit.event('source_hash_started', path=str(f))
        expected = capture_file(f, ctx.cancel_event) if ctx.audit else None
        if ctx.audit:
            ctx.audit.event('source_hash_finished', path=str(f), expected=expected)
        dest = move_file(f, folder, dry_run=ctx.dry_run, audit=ctx.audit, expected=expected, journal=ctx.journal, kind="junk")
        if not ctx.dry_run:
            ctx.record_move("junk", f, dest, expected)
        ctx.log(f"{ctx.prefix}[СЛУЖЕБНЫЙ] {f.name} → {JUNK_DIR}/")
        stats["junk"] += 1
        return 'junk'

    tag, folder, reason = _plan(ctx, f)
    if ctx.audit:
        ctx.audit.event('file_decision', path=str(f), category=tag, destination=str(folder) if folder else None,
                        reason=reason or 'extension_category_enabled', extension=f.suffix.lower())
    if folder is None:
        ctx.log(f"[ПРОПУСК] {reason}: {f.name}")
        if ctx.audit:
            ctx.audit.event("file_skipped", path=str(f), reason=reason)
        stats["skipped"] += 1
        return 'skipped'

    if ctx.audit:
        ctx.audit.event('source_hash_started', path=str(f))
    expected = capture_file(f, ctx.cancel_event) if ctx.audit else None
    if ctx.audit:
        ctx.audit.event('source_hash_finished', path=str(f), expected=expected)
    dest = move_file(f, folder, dry_run=ctx.dry_run, audit=ctx.audit, expected=expected, journal=ctx.journal, kind="sort")
    if not ctx.dry_run:
        ctx.record_move("sort", f, dest, expected)
    ctx.log(f"{ctx.prefix}[{tag}] {f.name} → {folder.relative_to(ctx.tgt).as_posix()}/")
    stats["moved"] += 1
    return 'moved'


def sort_files(ctx: Context) -> dict:
    stats = {"moved": 0, "skipped": 0, "errors": 0, "junk": 0}

    # список собирается заранее; архивы (в т.ч. оставшиеся нераспакованными) не трогаем
    files, blocked = [], []
    for f in source_files(ctx.src, ctx.audit, blocked, ctx.cancel_event):
        eligible = f.is_file() and not is_archive_like(f) and f.name.casefold() != EXTRACTION_STATE_FILE.casefold()
        if ctx.audit:
            ctx.audit.event('file_discovered', path=str(f), eligible=eligible,
                            reason='sort_candidate' if eligible else 'archive_or_internal_metadata_or_non_regular_file')
        if eligible:
            files.append(f)
    for path, reason in blocked:
        stats['skipped'] += 1
        ctx.log(f'[ПРОПУСК] Неподтверждённая распаковка не сортируется: {path} ({reason})', True)
        if ctx.audit:
            ctx.audit.check('Неподтверждённая распаковка не сортируется', None, {'path': str(path), 'reason': reason})
    total = len(files)
    processed = 0
    if ctx.audit:
        ctx.audit.event('sort_discovery_finished', candidates=total, complete=not ctx.cancelled)
    if total == 0:
        ctx.log("[ИНФО] Файлы для сортировки не найдены.")
    else:
        ctx.log(f"[СОРТИРОВКА] Файлов к обработке: {total}")

    for i, f in enumerate(files, 1):
        if ctx.cancelled:
            ctx.log("[СИСТЕМА] Остановлено пользователем.")
            break
        try:
            if ctx.audit:
                ctx.audit.event('file_processing_started', path=str(f), index=i, total=total)
            outcome = _handle(ctx, f, stats)
        except ReportWriteError:
            raise
        except ReportCancelled:
            break
        except Exception as exc:
            if ctx.audit:
                ctx.audit.exception(exc, operation='sort_file', path=str(f))
                ctx.audit.check("Сортировка файла выполнена", False, {"path": str(f), "error": str(exc)})
            ctx.log(f"[ОШИБКА] {f.name}: {exc}", True)
            stats["errors"] += 1
            outcome = 'error'
        processed += 1
        if ctx.audit:
            ctx.audit.event('file_processing_finished', path=str(f), outcome=outcome, counters=dict(stats))
        ctx.progress(i / total)

    if ctx.audit:
        for f in files[processed:]:
            ctx.audit.event('file_not_processed', path=str(f), reason='cancelled')
        ctx.audit.check('Учёт кандидатов сортировки', None if ctx.cancelled else processed == total,
                        {'discovered': total, 'processed': processed, 'not_processed': total - processed, 'counters': dict(stats)})
    if not ctx.dry_run:
        cleanup_empty_dirs(ctx.src, ctx.log, audit=ctx.audit, skip=incomplete_extraction_reason)
    return stats
