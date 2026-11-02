"""Общий контекст запуска: пути, опции, колбэки, отмена, журнал."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..utils.journal import Journal
from ..utils.reporting import ReportWriteError


@dataclass
class Options:
    media: bool = True
    docs: bool = True
    audio: bool = True
    other: bool = True               # неопознанные файлы → Other_Unsorted/<расширение>
    archive_action: str = "move"     # "move" | "delete" (после успешной распаковки)
    dry_run: bool = False            # ничего не менять, только показать план


class Context:
    def __init__(self, src: Path, tgt: Path, quar: Path, options: Options,
                 log_cb=None, progress_cb=None, cancel_event=None,
                 journal: Journal | None = None, log_file: Path | None = None, audit=None):
        self.src, self.tgt, self.quar = src, tgt, quar
        self.options = options
        self.audit = audit
        self.cancel_event = cancel_event or threading.Event()
        self.journal = journal or Journal(None)
        self._log_cb = log_cb
        self._progress_cb = progress_cb
        self._log_fh = open(log_file, "a", encoding="utf-8") if log_file else None

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def dry_run(self) -> bool:
        return self.options.dry_run

    @property
    def prefix(self) -> str:
        return "[ПРОБНЫЙ] " if self.options.dry_run else ""

    def log(self, message: str, is_error: bool = False) -> None:
        if self.audit:
            self.audit.log(message, is_error)
        elif self._log_cb:
            self._log_cb(message, is_error)
        if self._log_fh:
            self._log_fh.write(f"{datetime.now():%H:%M:%S} {message}\n")
            self._log_fh.flush()

    def record_move(self, kind, source, destination, expected):
        source, destination = Path(source).absolute(), Path(destination).absolute()
        try:
            self.journal.record(kind, source, destination, expected=expected)
        except OSError as exc:
            if self.audit:
                self.audit.record_move(source, destination, expected)
            raise ReportWriteError("Не удалось записать журнал перемещений; обработка остановлена") from exc
        if self.audit:
            self.audit.record_move(source, destination, expected)

    def record_delete(self, source, expected):
        try:
            self.journal.record("deleted", source, None, expected=expected)
        except OSError as exc:
            if self.audit:
                self.audit.record_delete(source, expected)
            raise ReportWriteError("Не удалось записать удаление в журнал; обработка остановлена") from exc
        if self.audit:
            self.audit.record_delete(source, expected)

    def progress(self, value: float) -> None:
        if self._progress_cb:
            self._progress_cb(max(0.0, min(1.0, value)))

    def close(self) -> None:
        self.journal.close()
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
