"""Журнал перемещений с проверкой содержимого и восстановлением после сбоя."""
from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .file_ops import move_file, unique_dest
from .reporting import ReportCancelled, ReportWriteError, capture_file, exception_details

_FIELDS = ['kind', 'source', 'destination', 'version', 'size', 'sha256', 'operation_id', 'action_kind']
_ACTIONS = {'sort', 'junk', 'duplicate'}


def _persist(writer, fh, kind, source, destination, expected=None, operation_id=None, action_kind=None):
    expected = expected or {}
    writer.writerow([kind, str(source), str(destination or ''), '2', expected.get('size', ''),
                     expected.get('sha256', ''), operation_id or '', action_kind or ''])
    fh.flush()
    os.fsync(fh.fileno())


def _baseline(row):
    try:
        size = int(row.get('size') or '')
        digest = row.get('sha256') or ''
        if size < 0 or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            return None
        return {'size': size, 'sha256': digest}
    except (ValueError, TypeError):
        return None


def _matches(actual, expected):
    return all(actual.get(k) == expected[k] for k in ('size', 'sha256'))


class Journal:
    """Записывает намерения и результаты перемещений с принудительной записью на диск."""

    def __init__(self, folder: Path | None):
        self.path = None
        self._fh = self._writer = None
        self._prepared = {}
        if folder is not None:
            folder.mkdir(parents=True, exist_ok=True)
            self.path = folder / f'moves_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid4().hex[:8]}.csv'
            self._fh = open(self.path, 'x', newline='', encoding='utf-8-sig')
            self._writer = csv.writer(self._fh)
            self._writer.writerow(_FIELDS)
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def prepare_move(self, kind, source, destination, expected):
        """Зафиксировать намерение до изменения содержимого файлов."""
        source, destination = Path(source).absolute(), Path(destination).absolute()
        op_id = uuid4().hex
        if self._writer:
            _persist(self._writer, self._fh, 'move_intent', source, destination, expected, op_id, kind)
        self._prepared[(str(source), str(destination), kind)] = op_id
        return op_id

    def record(self, kind, source, destination, expected=None, operation_id=None):
        if not self._writer:
            return
        if kind in _ACTIONS or kind in {"archive", "quarantine", "deleted"}:
            source = Path(source).absolute()
            destination = Path(destination).absolute() if destination is not None else None
        if kind in _ACTIONS and expected is None:
            candidate = Path(source) if Path(source).exists() else Path(destination)
            expected = capture_file(candidate) if candidate.exists() else None
        operation_id = operation_id or self._prepared.pop((str(source), str(destination), kind), None)
        _persist(self._writer, self._fh, kind, source, destination, expected, operation_id, kind)

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None


def _read_rows(path):
    with open(path, newline='', encoding='utf-8-sig') as fh:
        rows = list(csv.DictReader(fh))
    # Новые записи в старом трёхколоночном файле сохраняют дополнительные поля.
    for row in rows:
        extra = row.pop(None, None) or []
        for field, value in zip(_FIELDS[3:], extra):
            row[field] = value
    return rows


def _pending_rows(path: Path):
    """Найти неотменённые операции, включая безопасно восстановимые намерения."""
    rows = _read_rows(path)
    completed = set()
    active_duplicates = {}
    committed_ids = {r.get('operation_id') for r in rows if r.get('kind') in _ACTIONS and r.get('operation_id')}
    seen_commits = set()
    for index, row in enumerate(rows):
        pair = (row.get('source'), row.get('destination'))
        if row.get('kind') == 'duplicate':
            active_duplicates.setdefault(pair, []).append(index)
        elif row.get('kind') == 'duplicate_aborted':
            pending = active_duplicates.get(pair, [])
            if pending:
                completed.add(pending.pop())
        if row.get('kind') in _ACTIONS and row.get('operation_id'):
            if row['operation_id'] in seen_commits:
                completed.add(index)
            seen_commits.add(row['operation_id'])
    for row in rows:
        if row.get('kind') in {'undo', 'undo_unverified'}:
            try:
                completed.add(int(row.get('source', '')))
            except (TypeError, ValueError):
                pass
    legacy_targets = {}
    for row in rows:
        if row.get('kind') != 'undo_intent':
            continue
        try:
            index = int(row.get('source', ''))
            if not row.get('destination'):
                continue
            target = Path(row['destination'])
            expected = _baseline(row)
            baseline = _baseline(rows[index]) if 0 <= index < len(rows) else None
            if (0 <= index < len(rows) and rows[index].get('destination')
                    and not Path(rows[index]['destination']).exists() and target.is_file()):
                if expected and (baseline == expected or not rows[index].get('version')):
                    if _matches(capture_file(target), expected):
                        completed.add(index)
                elif not row.get('version') and not rows[index].get('version'):
                    legacy_targets[index] = str(target)
        except (ValueError, TypeError, KeyError, OSError):
            pass
    pending = []
    for index, row in enumerate(rows):
        if index in completed or not row.get('source') or not row.get('destination'):
            continue
        if row.get('kind') in _ACTIONS:
            pending.append((index, dict(row, _legacy_undo_target=legacy_targets[index])
                            if index in legacy_targets else row))
        elif (row.get('kind') == 'move_intent' and row.get('action_kind') in _ACTIONS
              and row.get('operation_id') not in committed_ids and not Path(row['source']).exists()):
            pending.append((index, dict(row, kind=row['action_kind'])))
    return pending


def find_latest_journal(folder: Path):
    files = sorted(folder.glob('moves_*.csv'), reverse=True) if folder.is_dir() else []
    return next((path for path in files if _pending_rows(path)), None)


def undo_journal(path: Path, log, cancel_event=None, audit=None):
    """Вернуть неизменённые файлы и сохранить результаты для повторной попытки."""
    rows = _pending_rows(path)
    if audit:
        audit.event('undo_journal_loaded', path=str(path), pending_rows=len(rows))
    restored = missing = errors = 0
    with open(path, 'a', newline='', encoding='utf-8-sig') as fh:
        writer = csv.writer(fh)
        for index, row in reversed(rows):
            if cancel_event and cancel_event.is_set():
                if audit:
                    audit.event('undo_cancelled', path=str(path), restored=restored, missing=missing, errors=errors)
                break
            original, current = Path(row['source']), Path(row['destination'])
            if audit:
                audit.event('undo_row_started', row=index, source=str(current), requested_destination=str(original),
                            original_operation_id=row.get('operation_id'), kind=row.get('kind'))
            legacy_target = row.get('_legacy_undo_target')
            if legacy_target and not current.exists() and Path(legacy_target).is_file():
                if audit:
                    audit.check('Историческое восстановление не подтверждено', None,
                                {'source': str(current), 'target': legacy_target,
                                 'reason': 'В старом журнале нет контрольной суммы'})
                log(f'[Откат] Историческое восстановление не подтверждено: {legacy_target}', is_error=True)
                try:
                    _persist(writer, fh, 'undo_unverified', index, legacy_target)
                except ReportWriteError:
                    raise
                except OSError as exc:
                    log(f'[Откат] Не удалось записать результат: {exc}', is_error=True)
                    errors += 1
                    break
                continue
            if not current.exists():
                if audit:
                    audit.check('Файл для отката доступен', False, {'path': str(current)})
                log(f'[Откат] Файл не найден: {current}', is_error=True)
                missing += 1
                continue
            try:
                actual = capture_file(current, cancel_event)
                expected = _baseline(row)
                if expected is None and row.get('version'):
                    if audit:
                        audit.check('Контрольная сумма исходного перемещения', False, {'path': str(current), 'reason': 'Некорректная контрольная сумма в журнале'})
                    log(f'[Откат] Некорректная контрольная сумма в журнале: {current}', is_error=True)
                    errors += 1
                    continue
                if expected is None:
                    if audit:
                        audit.check('Контрольная сумма исходного перемещения', None, {'path': str(current), 'reason': 'В старом журнале нет контрольной суммы'})
                    log(f'[Откат] Нет исходной контрольной суммы: {current}', is_error=True)
                elif not _matches(actual, expected):
                    if audit:
                        audit.check('Контрольная сумма файла для отката', False, {'path': str(current), 'expected': expected, 'actual': actual})
                    log(f'[Откат] Файл изменён: {current}', is_error=True)
                    errors += 1
                    continue
                original.parent.mkdir(parents=True, exist_ok=True)
                if audit:
                    audit.event('planned_undo', source=str(current), destination=str(unique_dest(original.parent, original.name)), expected=actual)
                if cancel_event and cancel_event.is_set():
                    break
                def on_reserved(target, evidence):
                    _persist(writer, fh, 'undo_intent', index, target, evidence, row.get('operation_id'), row.get('kind'))
                target = move_file(current, original.parent, audit=audit, expected=actual,
                                   filename=original.name, cancel_event=cancel_event, on_reserved=on_reserved)
                restored += 1
                if audit:
                    audit.record_move(current, target, actual)
            except ReportWriteError:
                raise
            except ReportCancelled:
                break
            except Exception as exc:
                if audit:
                    audit.event('exception', **exception_details(exc), operation='undo_file', path=str(current), requested_destination=str(original))
                log(f'[Откат] {current.name}: {exc}', is_error=True)
                errors += 1
                continue
            try:
                _persist(writer, fh, 'undo', index, target, actual, row.get('operation_id'), row.get('kind'))
            except ReportWriteError:
                raise
            except OSError as exc:
                if audit:
                    audit.event('exception', **exception_details(exc), operation='undo_journal_commit', path=str(path), row=index)
                log(f'[Откат] Не удалось записать результат: {exc}', is_error=True)
                errors += 1
                break
    if not missing and not errors and not _pending_rows(path):
        if audit:
            audit.event('undo_journal_completed', path=str(path), destination=str(path.with_name(path.name + '.undone')))
        path.rename(path.with_name(path.name + '.undone'))
    return restored, missing, errors
