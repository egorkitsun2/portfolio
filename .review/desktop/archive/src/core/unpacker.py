"""Распаковка архивов (7-Zip; ZIP — встроенными средствами, если 7-Zip нет)."""
from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
import traceback
import os
import json
import stat
from pathlib import Path
from typing import NamedTuple

from ..config import (ARCHIVE_EXTENSIONS, EXTRACTED_SUFFIX, EXTRACTION_STATE_FILE, MAX_NESTING,
                      PROCESSED_DIR, get_7z_path)
from ..utils.file_ops import move_file, unique_dir
from ..utils.reporting import ReportCancelled, ReportWriteError, capture_file
from .context import Context
from .archive_paths import UnsafeArchive, UninspectableArchive, check_archive_paths, verify_extracted_contents

# Заведомо неверный пароль: зашифрованный архив падает сразу, а не ждёт ввода.
_NO_PASSWORD = "ARCHIVARIUS_NO_PASSWORD"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # есть только в Windows

_PART_RAR = re.compile(r"^(?P<base>.+)\.part(?P<n>\d+)\.rar$", re.IGNORECASE)
_VOLUME = re.compile(r"^(?P<base>.+\.(?:7z|zip|rar))\.(?P<n>\d{3})$", re.IGNORECASE)


class _Result(NamedTuple):
    status: str   # "ok" | "warning" | "fail"
    reason: str = ""
    diagnostics: dict | None = None


# ---------- распознавание архивов ----------

def archive_role(path: Path) -> str | None:
    """'main' — архив или первый том, 'volume' — остальные тома, None — не архив."""
    for pattern in (_PART_RAR, _VOLUME):
        m = pattern.match(path.name)
        if m:
            return "main" if int(m["n"]) == 1 else "volume"
    return "main" if path.suffix.lower() in ARCHIVE_EXTENSIONS else None


def is_archive_like(path: Path) -> bool:
    return archive_role(path) is not None


def archive_stem(path: Path) -> str:
    m = _PART_RAR.match(path.name)
    if m:
        return m["base"]
    m = _VOLUME.match(path.name)
    if m:
        return Path(m["base"]).stem
    return path.stem


def volume_files(main: Path) -> list[Path]:
    """Тома одного комплекта. Неоднозначные первые тома требуют разных имён."""
    match = _PART_RAR.match(main.name)
    pattern = _PART_RAR
    if not match:
        match = _VOLUME.match(main.name)
        pattern = _VOLUME
    if not match:
        return [main]
    base, width = match["base"].casefold(), len(match["n"])
    candidates = []
    for path in main.parent.iterdir():
        other = pattern.match(path.name)
        if path.is_file() and other and other["base"].casefold() == base:
            candidates.append((path, other["n"]))
    if sum(int(number) == 1 for _, number in candidates) > 1:
        raise ValueError("Неоднозначные первые тома: задайте разным архивам разные базовые имена")
    if any(int(number) < 1 or str(int(number)).zfill(width) != number
           for _, number in candidates):
        raise ValueError("Несогласованная нумерация томов: проверьте имена и разделите комплекты архивов")
    return sorted(path for path, _ in candidates) or [main]


def _reserve_dir(parent: Path, name: str) -> Path:
    """Создаёт собственный каталог, не используя уже существующий."""
    parent.mkdir(parents=True, exist_ok=True)
    while True:
        path = unique_dir(parent, name)
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue


def extraction_state(directory: Path):
    """Служебная метка не лежит в данных архива: её имя запрещено preflight."""
    marker = Path(directory) / EXTRACTION_STATE_FILE
    if not os.path.lexists(marker):
        return None
    try:
        state = marker.lstat()
        if not stat.S_ISREG(state.st_mode) or getattr(state, 'st_file_attributes', 0) & 0x400:
            return 'invalid'
        with marker.open(encoding='utf-8') as stream:
            raw = stream.read(8193)
        data = json.loads(raw) if len(raw) <= 8192 else None
        if (isinstance(data, dict) and type(data.get('version')) is int and data['version'] == 1
                and isinstance(data.get('archive'), str) and data['archive'].strip()):
            if data.get('status') == 'pending':
                return 'pending'
            verification = data.get('verification')
            if (data.get('status') == 'complete' and isinstance(verification, dict)
                    and all(type(verification.get(k)) is int and verification[k] >= 0
                            for k in ('files', 'crc_verified', 'size_only'))
                    and verification['size_only'] == 0 and verification['crc_verified'] == verification['files']):
                return 'complete'
    except (OSError, ValueError):
        pass
    return 'invalid'


def _legacy_extracted(directory):
    return bool(re.search(re.escape(EXTRACTED_SUFFIX) + r'(?:-\d+)?$', Path(directory).name))


def incomplete_extraction_reason(path: Path):
    """Проверка предков также защищает прямой выбор подпапки незавершённого результата."""
    trusted = False
    for directory in reversed((Path(path), *Path(path).parents)):
        state = extraction_state(directory)
        if state == 'complete':
            trusted = True
        elif state is not None:
            return 'incomplete_or_invalid_extraction_state'
        elif _legacy_extracted(directory) and not trusted:
            return 'legacy_extraction_without_completion_evidence'
    return None


def _write_extraction_state(directory, archive, status, verification=None):
    try:
        with (directory / EXTRACTION_STATE_FILE).open('x' if status == 'pending' else 'w', encoding='utf-8') as stream:
            json.dump({'version': 1, 'status': status, 'archive': str(archive),
                       'verification': verification}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ReportWriteError('Не удалось сохранить состояние распаковки; результат заблокирован') from exc


def source_files(root, audit=None, blocked=None, cancel_event=None):
    """Обход источника без недостоверных результатов распаковки."""
    root = Path(root)
    def reject(path, reason):
        if blocked is not None:
            blocked.append((path, reason))
        if audit:
            audit.event('extraction_directory_blocked', path=str(path), reason=reason)
    reason = incomplete_extraction_reason(root)
    if reason:
        reject(root, reason)
        return
    def scan_error(exc):
        if audit:
            audit.exception(exc, operation='source_discovery', root=str(root))
            audit.check('Чтение каталога источника', False, {'path': exc.filename, 'error': str(exc)})
        else:
            raise exc
    for parent, dirs, names in os.walk(root, onerror=scan_error, followlinks=False):
        if cancel_event and cancel_event.is_set():
            return
        for name in dirs[:]:
            path = Path(parent) / name
            reason = incomplete_extraction_reason(path)
            if reason:
                dirs.remove(name)
                reject(path, reason)
        for name in names:
            if cancel_event and cancel_event.is_set():
                return
            yield Path(parent) / name


def _move_parts(ctx: Context, parts: list[Path], folder: Path, kind: str, evidence=None) -> None:
    # Все тома проверяются до первого перемещения. После начала комплект
    # завершается целиком, даже если пользователь запросил остановку.
    if ctx.audit:
        evidence = evidence or {part: capture_file(part, ctx.cancel_event) for part in parts}
        for part in parts:
            _check_unchanged(ctx, part, evidence[part])
    main = parts[0]
    if _PART_RAR.match(main.name) or _VOLUME.match(main.name):
        folder = _reserve_dir(folder, archive_stem(main) + "_volumes")
    for part in parts:
        expected = evidence[part] if evidence is not None else None
        dest = move_file(part, folder, audit=ctx.audit, expected=expected, journal=ctx.journal, kind=kind)
        ctx.record_move(kind, part, dest, expected)


def _check_unchanged(ctx, path, expected):
    actual = capture_file(path, ctx.cancel_event)
    unchanged = all(actual[key] == expected[key] for key in ("size", "sha256", "mtime_ns"))
    if ctx.audit:
        ctx.audit.event('archive_source_verified', path=str(path), expected=expected, actual=actual, passed=unchanged)
    if not unchanged:
        if ctx.audit:
            ctx.audit.check("Исходный архив не изменился", False,
                        {"path": str(path), "expected": expected, "actual": actual})
        raise OSError(f"Архив изменился во время обработки и сохранён на месте: {path}")


def find_archives(root: Path, audit=None, blocked=None) -> list[Path]:
    files = []
    token = getattr(audit, '_cancel_event', None)
    for path in source_files(root, audit, blocked, token):
        role = archive_role(path)
        if role and path.is_file():
            if audit:
                audit.event('archive_discovered', path=str(path), role=role, root=str(root))
            if role == 'main':
                files.append(path)
    return sorted(files)


# ---------- распаковка ----------

def _short_reason(output: str) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    errs = [ln for ln in lines
            if ln.upper().startswith(("ERROR", "WARNING")) or "can not" in ln.lower()]
    return "; ".join((errs or lines)[:2])[:200] or "неизвестная ошибка"


def _extract_7z(sz: str, arc: Path, out_dir: Path) -> _Result:
    cmd = [sz, "x", str(arc), f"-o{out_dir}", "-y", "-bd", f"-p{_NO_PASSWORD}"]
    res = subprocess.run(
        cmd, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, errors="replace", creationflags=_NO_WINDOW,
    )
    diagnostics = {'command': cmd, 'returncode': res.returncode,
                   'stdout': res.stdout[:65536], 'stderr': res.stderr[:65536],
                   'stdout_chars': len(res.stdout), 'stderr_chars': len(res.stderr),
                   'stdout_truncated': len(res.stdout) > 65536, 'stderr_truncated': len(res.stderr) > 65536}
    if res.returncode == 0:
        return _Result("ok", diagnostics=diagnostics)
    output = f"{res.stderr}\n{res.stdout}"
    if res.returncode == 1:  # 7-Zip: предупреждение, архив извлечён не полностью
        return _Result("warning", _short_reason(output), diagnostics)
    if re.search(r"wrong password|encrypted|enter password", output, re.IGNORECASE):
        return _Result("fail", "защищён паролем", diagnostics)
    return _Result("fail", _short_reason(output), diagnostics)


def _extract_zip(arc: Path, out_dir: Path) -> _Result:
    try:
        with zipfile.ZipFile(arc) as zf:
            zf.extractall(out_dir)  # zipfile сам защищает от выхода за пределы папки
        return _Result("ok")
    except Exception as exc:
        text = str(exc)
        diagnostics = {'exception_type': type(exc).__name__, 'error': text, 'traceback': traceback.format_exc()}
        if "password" in text.lower() or "encrypted" in text.lower():
            return _Result("fail", "защищён паролем", diagnostics)
        return _Result("fail", f"{type(exc).__name__}: {text}"[:200], diagnostics)


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _process_one(ctx: Context, arc: Path, sz: str | None, stats: dict) -> list[Path]:
    """Распаковывает один архив, возвращает найденные внутри вложенные архивы."""
    try:
        parts = volume_files(arc)
    except ValueError as exc:
        if ctx.audit:
            ctx.audit.exception(exc, operation='archive_volumes', path=str(arc))
            ctx.audit.event('archive_skipped', path=str(arc), reason=str(exc))
        ctx.log(f"[ПРОПУСК] {arc.name}: {exc}", True)
        stats["skipped"] += 1
        return []
    if ctx.audit:
        ctx.audit.event('archive_group', archive=str(arc), parts=[str(p) for p in parts])
    if ctx.dry_run:
        ctx.log(f"{ctx.prefix}Будет распакован: {_rel(ctx.src, arc)}")
        return []

    if sz:
        extractor = lambda out: _extract_7z(sz, arc, out)
    elif arc.suffix.lower() == ".zip":
        extractor = lambda out: _extract_zip(arc, out)
    else:
        if ctx.audit:
            ctx.audit.event('archive_skipped', path=str(arc), reason='7zip_unavailable_for_format', extension=arc.suffix)
        ctx.log(f"[ПРОПУСК] Для {arc.suffix} нужен 7-Zip, архив не тронут: {arc.name}", True)
        stats["skipped"] += 1
        return []

    if ctx.audit:
        ctx.audit.event('archive_hash_started', parts=[str(p) for p in parts])
    evidence = {part: capture_file(part, ctx.cancel_event) for part in parts}
    if ctx.audit:
        ctx.audit.event('archive_hash_finished', evidence={str(p): value for p, value in evidence.items()})
    try:
        check_archive_paths(arc, sz, audit=ctx.audit)
        for part in parts:
            _check_unchanged(ctx, part, evidence[part])
    except UninspectableArchive as exc:
        ctx.log(f"[КАРАНТИН] {arc.name}: оглавление недоступно: {exc}", True)
        if ctx.audit:
            ctx.audit.exception(exc, operation='archive_preflight', path=str(arc), decision='quarantine')
            ctx.audit.check("Оглавление архива доступно", None, {"archive": str(arc), "detail": str(exc)})
        _move_parts(ctx, parts, ctx.quar, "quarantine", evidence)
        stats["quarantined"] += 1
        return []
    except UnsafeArchive as exc:
        ctx.log(f"[ПРЕДУПРЕЖДЕНИЕ] {arc.name}: проверка оглавления: {exc}. Оригинал сохранён.", True)
        if ctx.audit:
            ctx.audit.exception(exc, operation='archive_preflight', path=str(arc), decision='retain')
            ctx.audit.check("Безопасность путей архива", False, {"archive": str(arc), "detail": str(exc)})
        stats["skipped"] += 1
        return []
    if ctx.audit:
        ctx.audit.event('extraction_directory_planned', archive=str(arc), parent=str(arc.parent), name_prefix=archive_stem(arc) + EXTRACTED_SUFFIX)
    out_dir = _reserve_dir(arc.parent, archive_stem(arc) + EXTRACTED_SUFFIX)
    try:
        _write_extraction_state(out_dir, arc, 'pending')
        if ctx.audit:
            ctx.audit.event("extraction_started", archive=str(arc), parts=[str(p) for p in parts], output=str(out_dir),
                            engine=str(sz or "python.zipfile"), command=[sz, 'x', str(arc), f'-o{out_dir}', '-y', '-bd', f'-p{_NO_PASSWORD}'] if sz else None)
    except ReportWriteError:
        # Движок ещё не запускался; удалить можно только собственную метку и пустую папку.
        try:
            if list(out_dir.iterdir()) == [out_dir / EXTRACTION_STATE_FILE]:
                (out_dir / EXTRACTION_STATE_FILE).unlink()
            out_dir.rmdir()
        except OSError:
            pass
        raise
    ctx.log(f"[РАСПАКОВКА] {_rel(ctx.src, arc)}")
    result = extractor(out_dir)
    if ctx.audit:
        ctx.audit.event("extraction_result", archive=str(arc), output=str(out_dir), status=result.status, reason=result.reason,
                        diagnostics=result.diagnostics)
        ctx.audit.inventory(out_dir, 'extracted:' + str(arc))
        ctx.audit.check("Распаковка: статус инструмента", result.status == "ok" if result.status != "fail" else None,
                        {"archive": str(arc), "status": result.status, "reason": result.reason,
                         "scope": "Статус распаковщика; отдельная сверка содержимого выполняется перед подтверждением результата"})

    if result.status == "fail":
        if ctx.audit:
            ctx.audit.event('extraction_preserved_incomplete', path=str(out_dir), reason=result.reason)
        ctx.log(f"[КАРАНТИН] {arc.name}: {result.reason}", True)
        _move_parts(ctx, parts, ctx.quar, "quarantine", evidence)
        stats["quarantined"] += 1
        return []

    delete = ctx.options.archive_action == "delete"
    if result.status == "warning":
        ctx.log(f"[ПРЕДУПРЕЖДЕНИЕ] {arc.name}: распакован не полностью ({result.reason}). "
                f"Оригинал сохранён.", True)
        stats["warning"] += 1
        _move_parts(ctx, parts, ctx.tgt / PROCESSED_DIR, 'archive', evidence)
        return []  # Неполные файлы остаются заблокированы, оригинал сохранён.

    try:
        verification = verify_extracted_contents(arc, out_dir, sz, ctx.cancel_event)
        for part in parts:
            _check_unchanged(ctx, part, evidence[part])
        if verification['size_only']:
            if ctx.audit:
                ctx.audit.check('Целостность распаковки подтверждена', None,
                                {'archive': str(arc), 'verification': verification, 'reason': 'CRC недоступен'})
            ctx.log(f'[ПРЕДУПРЕЖДЕНИЕ] Нет CRC для проверки {arc.name}; оригинал и заблокированный результат сохранены.', True)
            stats['warning'] += 1
            return []
        if ctx.audit:
            ctx.audit.check('Целостность распаковки подтверждена', True, {'archive': str(arc), **verification})
        _write_extraction_state(out_dir, arc, 'complete', verification)
    except UnsafeArchive as exc:
        if ctx.audit:
            ctx.audit.exception(exc, operation='verify_extraction', path=str(arc))
            ctx.audit.check('Целостность распаковки подтверждена', False, {'archive': str(arc), 'error': str(exc)})
        ctx.log(f'[ОШИБКА] {arc.name}: {exc}; оригинал и заблокированный результат сохранены.', True)
        stats['errors'] += 1
        return []
    stats['ok'] += 1

    processed_dir = ctx.tgt / PROCESSED_DIR
    if ctx.audit:
        ctx.audit.event('archive_disposition', archive=str(arc), requested=ctx.options.archive_action,
                        effective='delete' if delete else 'move', reason='retain_original_after_warning' if result.status == 'warning' else 'extraction_status_ok')
    if delete:
        for part in parts:
            _check_unchanged(ctx, part, evidence[part])
        for part in parts:
            if ctx.audit:
                ctx.audit.event("planned_delete", path=str(part), expected=evidence[part])
            part.unlink()
            ctx.record_delete(part, evidence[part] if evidence is not None else None)
    else:
        _move_parts(ctx, parts, processed_dir, "archive", evidence)
    ctx.log(f"[УСПЕХ] {arc.name} → {'удалён' if delete else PROCESSED_DIR + '/'}")
    return find_archives(out_dir, ctx.audit)


def unpack_archives(ctx: Context) -> dict:
    stats = {"ok": 0, "warning": 0, "quarantined": 0, "skipped": 0, "errors": 0}
    blocked = []
    pending = find_archives(ctx.src, ctx.audit, blocked)
    for path, reason in blocked:
        stats['skipped'] += 1
        ctx.log(f'[ПРОПУСК] Неподтверждённый результат распаковки сохранён без обработки: {path} ({reason})', True)
        if ctx.audit:
            ctx.audit.check('Незавершённая распаковка оставлена без обработки', None, {'path': str(path), 'reason': reason})
    if not pending:
        ctx.log("[ИНФО] Архивы не найдены.")
        return stats

    sz = get_7z_path()
    if ctx.audit:
        ctx.audit.event('extractor_selected', seven_zip=sz, zip_fallback=not bool(sz), max_nesting=MAX_NESTING)
    if not sz and not ctx.dry_run:
        ctx.log("[ПРЕДУПРЕЖДЕНИЕ] 7-Zip не найден: распакую только .zip, "
                "а .7z/.rar будут пропущены.", True)
    ctx.log(f"[*] Найдено архивов: {len(pending)}")

    total, done = len(pending), 0
    for _depth in range(MAX_NESTING):
        next_round: list[Path] = []
        for arc_index, arc in enumerate(pending):
            if ctx.cancelled:
                if ctx.audit:
                    ctx.audit.event('archive_processing_cancelled', remaining=[str(p) for p in pending[arc_index:]], depth=_depth)
                ctx.log("[СИСТЕМА] Остановлено пользователем.")
                return stats
            done += 1
            if arc.exists():
                try:
                    if ctx.audit:
                        ctx.audit.event('archive_processing_started', path=str(arc), depth=_depth)
                    nested = _process_one(ctx, arc, sz, stats)
                    if ctx.audit:
                        ctx.audit.event('archive_processing_finished', path=str(arc), nested=[str(p) for p in nested], counters=dict(stats))
                    next_round.extend(nested)
                    total += len(nested)
                except ReportWriteError:
                    raise
                except ReportCancelled:
                    return stats
                except Exception as exc:  # сбой одного архива не останавливает остальные
                    if ctx.audit:
                        ctx.audit.exception(exc, operation='process_archive', path=str(arc), depth=_depth)
                        ctx.audit.check("Обработка архива выполнена", False, {"archive": str(arc), "error": str(exc)})
                    ctx.log(f"[ОШИБКА] {arc.name}: {exc}", True)
                    stats["errors"] += 1
            elif ctx.audit:
                ctx.audit.event('archive_skipped', path=str(arc), reason='no_longer_exists')
            ctx.progress(done / total)
        pending = next_round
        if not pending:
            break

    if pending:
        stats['skipped'] += len(pending)
        if ctx.audit:
            for arc in pending:
                ctx.audit.event('archive_skipped', path=str(arc), reason='max_nesting', max_nesting=MAX_NESTING)
        ctx.log(f"[ПРЕДУПРЕЖДЕНИЕ] Вложенность архивов глубже {MAX_NESTING} уровней: "
                f"{len(pending)} шт. оставлено без распаковки.", True)
    return stats
