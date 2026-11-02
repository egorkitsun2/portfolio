"""Поиск точных копий через Czkawka CLI и перенос с возможностью отката."""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .. import config
from ..utils.reporting import RunReport, ReportCancelled, ReportWriteError, capture_file
from ..config import DUPLICATES_DIR, JUNK_DIR, LOGS_DIR, PROCESSED_DIR, EXTRACTION_STATE_FILE
from .unpacker import incomplete_extraction_reason
from ..utils.journal import Journal, find_latest_journal, undo_journal


class ScanCancelled(Exception):
    """Поиск отменён пользователем."""


@dataclass(frozen=True)
class FileEntry:
    path: Path
    size: int
    fingerprint: tuple[int, int, int, int]


@dataclass(frozen=True)
class DuplicateGroup:
    files: tuple[FileEntry, ...]


@dataclass(frozen=True)
class ScanResult:
    groups: tuple[DuplicateGroup, ...]
    reclaimable_bytes: int


@dataclass(frozen=True)
class MoveResult:
    moved: int
    skipped: int
    errors: int
    journal: Path | None


_PROTECTED = {x.casefold() for x in (DUPLICATES_DIR, JUNK_DIR, LOGS_DIR, PROCESSED_DIR, EXTRACTION_STATE_FILE)}
_ARCHIVE = re.compile(r'(?:\.zip|\.7z|\.rar|\.r\d\d|\.z\d\d|\.7z\.\d+|\.zip\.\d+|\.part\d+\.rar)$', re.I)


def get_czkawka_path() -> str | None:
    return shutil.which('czkawka_cli')


def _log(cb, message, is_error=False):
    if cb:
        cb(message, is_error=is_error)


def _inside(path, root):
    return path == root or root in path.parents


def _protected(path):
    return any(part.casefold() in _PROTECTED for part in path.parts) or bool(incomplete_extraction_reason(path))


def _fingerprint(path):
    st = path.stat()
    return (st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino)


def _has_link(path):
    for part in (path, *path.parents):
        try:
            st = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            return True
    return False


def _canonical(path):
    path = Path(path).absolute()
    if '..' in path.parts or _has_link(path):
        raise ValueError('Путь содержит ссылку или переход в родительский каталог')
    return path.resolve()


def _ordinary(path):
    return path.is_file() and not _has_link(path)


def _cli_version(executable):
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run([str(executable), '--version'], stdout=output,
                                    stderr=subprocess.STDOUT, timeout=5,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            output.seek(0)
            version = output.read(4096).decode('utf-8', errors='replace').strip()
            if result.returncode:
                return {'value': None, 'reason': f'код возврата {result.returncode}', 'output': version}
            return {'value': version or None, 'reason': None if version else 'пустой вывод'}
    except (OSError, subprocess.SubprocessError) as exc:
        return {'value': None, 'reason': str(exc)}


def _scan_duplicates(roots, excluded=(), cancel_event=None, log_cb=None, executable=None, report=None):
    if cancel_event and cancel_event.is_set():
        raise ScanCancelled()
    roots = tuple(_canonical(p) for p in roots)
    if not roots or any(not p.is_dir() or _protected(p) or not _ordinary_dir(p) for p in roots):
        raise ValueError('Выберите существующие папки вне служебных каталогов и символических ссылок')
    exclusions = tuple(_canonical(p) for p in excluded)
    for root in roots:
        for exclusion in exclusions:
            if _inside(root, exclusion):
                report.event('root_filtered', path=str(root), reason='excluded', exclusion=str(exclusion))
                break
    roots = tuple(root for root in roots if not any(_inside(root, x) for x in exclusions))
    if not roots:
        report.check('Все выбранные каталоги исключены', True)
        return ScanResult((), 0)
    executable = executable or get_czkawka_path()
    if not executable:
        raise RuntimeError('Czkawka CLI не найдена в PATH')
    # Explicit directory exclusions avoid traversing reserved directories at any depth.
    blocked = set(exclusions)
    def discovery_error(exc):
        report.exception(exc, context='duplicate_discovery')
        report.check('Чтение каталога поиска дубликатов', False, {'path': exc.filename, 'error': str(exc)})
    for root in roots:
        for base, dirs, names in os.walk(root, followlinks=False, onerror=discovery_error):
            if cancel_event and cancel_event.is_set():
                raise ScanCancelled()
            for name in dirs[:]:
                path = Path(base) / name
                reason = ('protected' if _protected(path) else
                          'link' if _has_link(path) else
                          'excluded' if any(_inside(path, x) for x in exclusions) else None)
                if reason:
                    report.event('directory_filtered', path=str(path), reason=reason)
                    blocked.add(path)
                    dirs.remove(name)
            for name in names:
                _check_cancel(cancel_event)
                path = Path(base) / name
                report.event('duplicate_file_discovered', path=str(path), archive=bool(_ARCHIVE.search(name)),
                             scope='filesystem_discovery; CLI_has_additional_filters')
    try:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'results.json'
            def preserve_result(partial=False):
                present = output.exists()
                if present:
                    try:
                        shutil.copyfile(output, report.directory / 'czkawka_result.json')
                    except OSError as exc:
                        raise ReportWriteError('Не удалось сохранить исходный JSON Czkawka') from exc
                report.event('cli_artifacts', output='czkawka_cli.log', result='czkawka_result.json' if present else None, partial=partial)
            args = [str(executable), 'dup']
            for root in roots:
                args += ['-d', str(root)]
            if blocked:
                for directory in blocked:
                    args += ['-e', str(directory)]
            # Основные форматы исключаем до хеширования; тома также проверяем после поиска.
            for pattern in ('*.zip', '*.7z', '*.rar'):
                args += ['-E', pattern]
            args += ['-m', '1', '-s', 'HASH', '-t', 'BLAKE3', '-D', 'NONE', '-H', '-W', '-N', '-C', str(output)]
            report.event('cli_start', executable=str(executable), args=args, cwd=os.getcwd(), version=_cli_version(executable))
            with (report.directory / 'czkawka_cli.log').open('w+b') as errors:
                proc = subprocess.Popen(args, stdout=errors, stderr=subprocess.STDOUT,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                while proc.poll() is None:
                    if cancel_event and cancel_event.wait(0.1):
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                        errors.seek(0)
                        raw = errors.read(4097)
                        report.event('cli_cancelled', exit_code=proc.returncode, output=raw[:4096].decode('utf-8', errors='replace'), output_limit_bytes=4096, output_truncated=len(raw) > 4096, stderr_merged=True)
                        preserve_result(partial=True)
                        raise ScanCancelled()
                    if not cancel_event:
                        time.sleep(0.1)
                errors.seek(0)
                raw = errors.read(4097)
                detail = raw[:4096].decode('utf-8', errors='replace')
                report.event('cli_result', exit_code=proc.returncode, output=detail, output_limit_bytes=4096, output_truncated=len(raw) > 4096, stderr_merged=True, cancelled=bool(cancel_event and cancel_event.is_set()))
                preserve_result(partial=proc.returncode != 0 or bool(cancel_event and cancel_event.is_set()))
                if cancel_event and cancel_event.is_set():
                    raise ScanCancelled()
                report.check('Запуск Czkawka завершён успешно', proc.returncode == 0, {'код возврата': proc.returncode})
                if proc.returncode:
                    raise RuntimeError(f'Czkawka CLI завершилась с ошибкой {proc.returncode}; проверьте версию: {detail}')
            try:
                data = json.loads(output.read_text(encoding='utf-8-sig'))
                if not isinstance(data, dict):
                    raise ValueError('expected object')
                groups = []
                seen = set()
                for lists in data.values():
                    if not isinstance(lists, list):
                        raise ValueError('expected group list')
                    for entries in lists:
                        if not isinstance(entries, list):
                            raise ValueError('expected group')
                        files = []
                        for item in entries:
                            path = Path(item['path'])
                            if not path.is_absolute():
                                raise ValueError('relative path')
                            try:
                                path = _canonical(path)
                            except ValueError as exc:
                                report.event('path_filtered', path=str(path), reason='invalid_path', error=str(exc))
                                continue
                            reason = ('already_seen' if path in seen else
                                      'outside_roots' if not any(_inside(path, root) for root in roots) else
                                      'protected' if _protected(path) else
                                      'excluded' if any(_inside(path, x) for x in exclusions) else
                                      'not_ordinary_file' if not _ordinary(path) else None)
                            if reason:
                                report.event('path_filtered', path=str(path), reason=reason)
                                continue
                            fingerprint = _fingerprint(path)
                            if fingerprint[0] != item['size']:
                                report.event('path_filtered', path=str(path), reason='size_changed', reported_size=item['size'], actual_size=fingerprint[0])
                                continue
                            if _ARCHIVE.search(path.name):
                                report.event('path_filtered', path=str(path), reason='archive')
                                _log(log_cb, f'Архив исключён из поиска дубликатов: {path}')
                                continue
                            seen.add(path)
                            files.append(FileEntry(path, fingerprint[0], fingerprint))
                        if len(files) > 1:
                            files.sort(key=lambda entry: os.path.normcase(str(entry.path)))
                            groups.append(DuplicateGroup(tuple(files)))
                            report.event('duplicate_group_found', files=[{'path': str(f.path), 'size': f.size, 'fingerprint': f.fingerprint} for f in files])
                        else:
                            report.event('duplicate_group_filtered', reason='fewer_than_two_files', files=[str(f.path) for f in files])
            except ReportWriteError:
                raise
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise RuntimeError(f'Некорректный JSON Czkawka CLI: {exc}') from exc
    except FileNotFoundError as exc:
        raise RuntimeError(f'Czkawka CLI не найдена: {executable}') from exc
    if cancel_event and cancel_event.is_set():
        raise ScanCancelled()
    groups.sort(key=lambda group: (-group.files[0].size, str(group.files[0].path)))
    _log(log_cb, f"[ДУБЛИКАТЫ] Найдено групп: {len(groups)}")
    return ScanResult(tuple(groups), sum(sum(f.size for f in g.files[1:]) for g in groups))


def _ordinary_dir(path):
    return not _has_link(path)


def _storage(storage):
    path = _canonical(storage)
    if path.name != DUPLICATES_DIR or not _ordinary_dir(path) or _has_link(path / LOGS_DIR):
        raise ValueError(f'Каталог хранения должен называться {DUPLICATES_DIR} и не содержать ссылок')
    return path


def _check_cancel(cancel_event):
    if cancel_event and cancel_event.is_set():
        raise ScanCancelled()


def _same_bytes(first, second, cancel_event=None):
    with first.open('rb') as a, second.open('rb') as b:
        while True:
            _check_cancel(cancel_event)
            left, right = a.read(1024 * 1024), b.read(1024 * 1024)
            if left != right:
                return False
            if not left:
                return True


def _compare_and_capture(first, second, cancel_event=None):
    """Сравнить два стабильных файла и получить доказательства за один проход."""
    first, second = Path(first).absolute(), Path(second).absolute()
    def fingerprint(st):
        return st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino
    before = (first.stat(), second.stat())
    digest = hashlib.sha256()
    with first.open('rb') as a, second.open('rb') as b:
        opened = (os.fstat(a.fileno()), os.fstat(b.fileno()))
        if any(fingerprint(x) != fingerprint(y) for x, y in zip(before, opened)):
            raise OSError('Файл изменился перед сравнением')
        while True:
            _check_cancel(cancel_event)
            left, right = a.read(1024 * 1024), b.read(1024 * 1024)
            if left != right:
                raise ValueError('Содержимое файлов различается')
            if not left:
                break
            digest.update(left)
        after_open = (os.fstat(a.fileno()), os.fstat(b.fileno()))
    after = (first.stat(), second.stat())
    if any(fingerprint(initial) != fingerprint(final) for initial, final in zip(before, after_open)) or any(
            fingerprint(initial) != fingerprint(final) for initial, final in zip(before, after)):
        raise OSError('Файл изменился во время сравнения')
    hexdigest = digest.hexdigest()
    return tuple({'path': str(path), 'size': st.st_size, 'sha256': hexdigest, 'mtime_ns': st.st_mtime_ns}
                 for path, st in zip((first, second), after))


def _copy_bytes(source, target, cancel_event=None):
    while True:
        _check_cancel(cancel_event)
        chunk = source.read(1024 * 1024)
        if not chunk:
            return
        target.write(chunk)


def _move_duplicates(result, keepers, storage, cancel_event=None, log_cb=None, report=None):
    storage = _storage(storage)
    moved = skipped = errors = 0
    journal = None
    try:
        for index, group in enumerate(result.groups):
            report.event('duplicate_group_selection', group=index, files=[str(f.path) for f in group.files], keeper=str(keepers[index]) if index in keepers else None)
            if cancel_event and cancel_event.is_set():
                report.event('duplicate_group_skipped', group=index, reason='cancelled')
                break
            if index not in keepers:
                report.event('duplicate_group_skipped', group=index, reason='keeper_not_selected')
                skipped += max(0, len(group.files) - 1)
                continue
            try:
                keeper_path = _canonical(keepers[index])
            except (ValueError, OSError) as exc:
                report.exception(exc, context='keeper_path', group=index, path=str(keepers[index]))
                keeper_path = None
            keeper = next((f for f in group.files if f.path == keeper_path), None)
            if keeper is None:
                report.event('duplicate_group_skipped', group=index, reason='invalid_keeper', keeper=str(keepers[index]))
                report.check('Выбран сохраняемый файл', False, {'group': index})
                errors += 1
                _log(log_cb, f'Не выбран файл для сохранения в группе {index}', True)
                continue
            for entry in group.files:
                if entry == keeper:
                    continue
                if cancel_event and cancel_event.is_set():
                    break
                transfer_id = uuid4().hex
                target = None
                try:
                    report.event('duplicate_recheck_started', group=index, transfer_id=transfer_id, path=str(entry.path), keeper=str(keeper.path), fingerprint=entry.fingerprint, keeper_fingerprint=keeper.fingerprint)
                    if _inside(entry.path, storage) or not _ordinary(entry.path) or not _ordinary(keeper.path) or _fingerprint(keeper.path) != keeper.fingerprint or _fingerprint(entry.path) != entry.fingerprint:
                        raise ValueError('файл изменён или недоступен')
                    keeper_evidence, expected = _compare_and_capture(keeper.path, entry.path, cancel_event)
                    if _fingerprint(keeper.path) != keeper.fingerprint or _fingerprint(entry.path) != entry.fingerprint:
                        raise ValueError('Файл изменился во время сравнения')
                    report.event('duplicate_recheck_passed', group=index, path=str(entry.path), keeper=str(keeper.path), evidence=expected, keeper_evidence=keeper_evidence)
                    storage.mkdir(parents=True, exist_ok=True)
                    if journal is None:
                        journal = Journal(storage / LOGS_DIR)
                    name = entry.path.name
                    counter = 0
                    while True:
                        target = storage / (name if counter == 0 else f'{Path(name).stem}-{counter}{Path(name).suffix}')
                        try:
                            out = target.open('xb')
                            break
                        except FileExistsError:
                            report.event('duplicate_target_conflict', source=str(entry.path), destination=str(target))
                            counter += 1
                    recorded = False
                    try:
                        with out:
                            journal.prepare_move('duplicate', entry.path, target, expected)
                            _audit_call(report.event, 'planned_copy', transfer_id=transfer_id, source=str(entry.path), destination=str(target), expected=expected, keeper=keeper_evidence)
                            report.event('copy_started', transfer_id=transfer_id, source=str(entry.path), destination=str(target), expected=expected)
                            with entry.path.open('rb') as source:
                                _copy_bytes(source, out, cancel_event)
                            out.flush()
                            os.fsync(out.fileno())
                        report.event('copy_finished', transfer_id=transfer_id, source=str(entry.path), destination=str(target), bytes_copied=target.stat().st_size)
                        shutil.copystat(entry.path, target)
                        report.event('destination_verification_started', transfer_id=transfer_id, destination=str(target), keeper=str(keeper.path))
                        equal = _same_bytes(target, keeper.path, cancel_event)
                        report.event('destination_verification_finished', transfer_id=transfer_id, destination=str(target), keeper=str(keeper.path), passed=equal, method='byte_comparison')
                        if not equal:
                            raise ValueError('копия не совпадает с сохранённым файлом')
                        if _fingerprint(entry.path) != entry.fingerprint or _fingerprint(keeper.path) != keeper.fingerprint:
                            raise ValueError('файл изменён во время копирования')
                        _check_cancel(cancel_event)
                        # Сохраняем копию при неопределённом состоянии журнала.
                        _audit_call(report.event, 'planned_move', transfer_id=transfer_id, source=str(entry.path), destination=str(target), expected=expected, keeper=keeper_evidence)
                        recorded = True
                        journal.record('duplicate', entry.path, target, expected=expected)
                        try:
                            _check_cancel(cancel_event)
                            report.event('source_delete_started', transfer_id=transfer_id, source=str(entry.path), destination=str(target), reason='copy_matches_keeper')
                            entry.path.unlink()
                        except (OSError, ScanCancelled):
                            try:
                                journal.record('duplicate_aborted', entry.path, target)
                                recorded = False
                            finally:
                                _audit_call(report.event, 'move_unlink_failed', source=str(entry.path), destination=str(target), source_exists=entry.path.exists(), destination_exists=target.exists())
                            raise
                    except Exception:
                        if not recorded:
                            target.unlink(missing_ok=True)
                        raise
                    _audit_call(report.record_move, entry.path, target, expected, keeper_evidence)
                    report.event('move_source_deleted', transfer_id=transfer_id, source=str(entry.path), destination=str(target))
                    moved += 1
                    _log(log_cb, f"[ДУБЛИКАТЫ] {entry.path} → {target}")
                except (ReportCancelled, ScanCancelled) as exc:
                    report.event('transfer_cancelled', transfer_id=transfer_id, source=str(entry.path),
                                 destination=str(target) if target else None, source_exists=entry.path.exists(),
                                 destination_exists=target.exists() if target else None)
                    if isinstance(exc, ReportCancelled):
                        raise ScanCancelled() from exc
                    raise
                except ReportWriteError:
                    raise
                except (OSError, ValueError) as exc:
                    report.exception(exc, context='duplicate_move', transfer_id=transfer_id, group=index, path=str(entry.path), keeper=str(keeper.path))
                    report.check('Проверка файла перед переносом', False, {'path': str(entry.path), 'error': str(exc)})
                    errors += 1
                    _log(log_cb, f'Не удалось переместить {entry.path}: {exc}', True)
    finally:
        if journal:
            journal.close()
    return MoveResult(moved, skipped, errors, journal.path if journal else None)


def _audit_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ReportWriteError:
        raise
    except OSError as exc:
        raise RuntimeError('Не удалось записать отчёт операции') from exc


def _report(operation, report_dir, log_cb, report_cb, metadata, cancel_event=None):
    return RunReport(report_dir if report_dir is not None else config.DATA_DIR / LOGS_DIR,
                     operation, metadata=metadata, log_cb=log_cb, report_cb=report_cb, cancel_event=cancel_event)


def scan_duplicates(roots, excluded=(), cancel_event=None, log_cb=None, executable=None,
                    report_dir=None, report_cb=None):
    roots, excluded = tuple(roots), tuple(excluded)
    report = _report('duplicate_scan', report_dir, log_cb, report_cb,
                     {'roots': [str(p) for p in roots], 'excluded': [str(p) for p in excluded],
                      'scope_exclusions': ['служебные каталоги', 'ссылки', 'архивы']}, cancel_event=cancel_event)
    status = 'completed'
    try:
        report.start_stage('Запуск и результат Czkawka')
        result = _scan_duplicates(roots, excluded, cancel_event, report.log, executable, report)
        report.end_stage('Запуск и результат Czkawka', {'groups': len(result.groups)})
        report.start_stage('Проверка результатов поиска')
        verified = []
        for index, group in enumerate(result.groups):
            captures = []
            report.event('duplicate_verification_started', group=index, files=[str(f.path) for f in group.files])
            try:
                for entry in group.files:
                    if not _ordinary(entry.path) or _fingerprint(entry.path) != entry.fingerprint:
                        raise OSError(f'Файл изменён или недоступен: {entry.path}')
                    captures.append(capture_file(entry.path, cancel_event))
                    if _fingerprint(entry.path) != entry.fingerprint:
                        raise OSError(f'Файл изменён во время проверки: {entry.path}')
                for entry in group.files:
                    if not _ordinary(entry.path) or _fingerprint(entry.path) != entry.fingerprint:
                        raise OSError(f'Файл изменён во время проверки группы: {entry.path}')
                equal = len({x['sha256'] for x in captures}) == 1
                report.check('Хеши файлов группы совпадают', equal,
                             {'группа': index, 'файлы': captures})
                if equal:
                    verified.append(group)
                report.event('duplicate_verification_result', group=index, accepted=equal, reason='hashes_match' if equal else 'hashes_differ', files=captures)
            except ReportWriteError:
                raise
            except OSError as exc:
                report.exception(exc, context='duplicate_verification', group=index, files=[str(f.path) for f in group.files])
                report.event('duplicate_verification_result', group=index, accepted=False, reason='file_error', error=str(exc))
                report.check('Хеши файлов группы совпадают', False,
                             {'группа': index, 'файлы': captures, 'ошибка': str(exc)})
        result = ScanResult(tuple(verified), sum(sum(f.size for f in g.files[1:]) for g in verified))
        report.end_stage('Проверка результатов поиска', {'groups': len(result.groups)})
        _check_cancel(cancel_event)
        return result
    except (ScanCancelled, ReportCancelled) as exc:
        status = 'cancelled'
        report.event('operation_cancelled', operation='duplicate_scan')
        if isinstance(exc, ReportCancelled):
            raise ScanCancelled() from exc
        raise
    except Exception as exc:
        status = 'failed'
        report.exception(exc, context='duplicate_scan')
        raise
    finally:
        report.finish(status)


def move_duplicates(result, keepers, storage, cancel_event=None, log_cb=None,
                    report_dir=None, report_cb=None):
    report = _report('duplicate_move', report_dir, log_cb, report_cb,
                     {'storage': str(storage), 'keepers': {str(k): str(v) for k, v in keepers.items()}}, cancel_event=cancel_event)
    status = 'completed'
    try:
        report.start_stage('Повторная проверка и перенос')
        outcome = _move_duplicates(result, keepers, storage, cancel_event, report.log, report)
        report.end_stage('Повторная проверка и перенос', {'moved': outcome.moved, 'skipped': outcome.skipped, 'errors': outcome.errors})
        if cancel_event and cancel_event.is_set():
            status = 'cancelled'
        return outcome
    except (ScanCancelled, ReportCancelled) as exc:
        status = 'cancelled'
        report.event('operation_cancelled', operation='duplicate_move')
        if isinstance(exc, ReportCancelled):
            raise ScanCancelled() from exc
        raise
    except Exception as exc:
        status = 'failed'
        report.exception(exc, context='duplicate_move')
        raise
    finally:
        report.finish(status)


def undo_duplicates(storage, log_cb=None, cancel_event=None, report_dir=None, report_cb=None):
    report = _report('duplicate_undo', report_dir, log_cb, report_cb, {'storage': str(storage)}, cancel_event=cancel_event)
    status = 'completed'
    try:
        report.start_stage('Откат переноса')
        path = find_latest_journal(_storage(storage) / LOGS_DIR)
        if path is None:
            result = (0, 0, 0)
        else:
            result = undo_journal(path, report.log, cancel_event=cancel_event, audit=report)
        report.check('Результат отката', result[1] == 0 and result[2] == 0,
                     {'восстановлено': result[0], 'отсутствуют': result[1], 'ошибки': result[2]})
        report.end_stage('Откат переноса', {'restored': result[0], 'skipped': result[1], 'errors': result[2]})
        if cancel_event and cancel_event.is_set():
            status = 'cancelled'
        return result
    except (ScanCancelled, ReportCancelled) as exc:
        status = 'cancelled'
        report.event('operation_cancelled', operation='duplicate_undo')
        if isinstance(exc, ReportCancelled):
            raise ScanCancelled() from exc
        raise
    except Exception as exc:
        status = 'failed'
        report.exception(exc, context='duplicate_undo')
        raise
    finally:
        report.finish(status)
