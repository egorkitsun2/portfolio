"""Графический интерфейс (customtkinter).

Рабочие потоки НИКОГДА не трогают виджеты: они кладут сообщения в очередь,
а главный поток раз в 100 мс забирает их пачкой (поэтому окно не тормозит
даже на десятках тысяч файлов).
"""
from __future__ import annotations

import os
import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from dataclasses import dataclass
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..config import PROCESSED_DIR, get_7z_path, load_settings, save_settings
from ..core.context import Options
from ..core.pipeline import run_pipeline, run_undo, validate_paths
from .duplicates import DuplicateEvent, DuplicatePanel

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

@dataclass(frozen=True)
class _ReportReady:
    path: Path


@dataclass(frozen=True)
class _ReportLoaded:
    request: int
    path: Path
    content: str = ""
    state: str | None = None
    error: str | None = None


_DONE = object()          # сигнал «рабочий поток завершён»
MAX_LOG_LINES = 3000      # старые строки лога обрезаются

ARCHIVE_CHOICES = {
    f"Переносить в {PROCESSED_DIR}": "move",
    "Удалять после распаковки": "delete",
}


class ArchiveSorterApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Архивариус: Мясорубка данных")
        self.geometry("880x780")
        self.minsize(780, 660)

        self._settings = load_settings()
        self._queue: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._progress = 0.0
        self._busy = False
        self._closing = False
        self._last_run_dry = None
        self._line_counts = {"log": 0, "err": 0}

        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(padx=5, pady=5, fill="both", expand=True)
        self._sort_page = self.tabs.add("Сортировка")
        duplicate_page = self.tabs.add("Дубликаты")
        report_page = self.tabs.add("Отчёты")
        self.last_report = None
        self._report_request = 0
        self._report_loading = False
        self._worker_done_waiting = False
        self._build_paths()
        self._build_options()
        self._build_logs()
        self._build_buttons()
        self.duplicates_panel = DuplicatePanel(duplicate_page, self)
        self.duplicates_panel.pack(fill="both", expand=True)
        self._build_report_page(report_page)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._check_dependencies()
        self.after(100, self._poll)

    # ---------- построение интерфейса ----------

    def _build_report_page(self, parent):
        self.report_path_var = tk.StringVar(value="")
        ctk.CTkLabel(parent, text="Последний автоматический отчёт", anchor="w").pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkEntry(parent, textvariable=self.report_path_var, state="readonly").pack(fill="x", padx=12, pady=4)
        buttons = ctk.CTkFrame(parent, fg_color="transparent")
        buttons.pack(fill="x", padx=12, pady=8)
        for text, command in (("Выбрать отчёт", self.choose_report), ("Обновить", self.refresh_report),
                              ("Открыть файл", lambda: self.open_report(False)),
                              ("Открыть папку", lambda: self.open_report(True))):
            ctk.CTkButton(buttons, text=text, command=command, width=145).pack(side="left", padx=(0, 8))
        self.report_status = ctk.CTkLabel(parent, text="После реальной операции здесь появится отчёт. "
                                         "В пробном режиме файлы отчётов не создаются.",
                                         anchor="w", wraplength=700, justify="left")
        self.report_status.pack(fill="x", padx=12, pady=(0, 8))
        self.report_box = ctk.CTkTextbox(parent, state="disabled", font=("Consolas", 12), wrap="word")
        self.report_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _on_report_ready(self, path):
        self._queue.put(_ReportReady(Path(path)))

    def choose_report(self):
        path = filedialog.askopenfilename(parent=self, title="Выберите report.txt",
                                          filetypes=[("Текстовые отчёты", "*.txt")])
        if path:
            self._show_report(Path(path))

    def refresh_report(self):
        if self.last_report:
            self._show_report(self.last_report)

    def _show_report(self, path):
        path = Path(path)
        self._report_request += 1
        request = self._report_request
        self._report_loading = True
        self.report_status.configure(text="Загрузка отчёта…")
        def worker():
            try:
                with path.open(encoding="utf-8") as stream:
                    content = stream.read(150001)
                if len(content) > 150000:
                    content = content[:150000] + "\n\nНа экране показана часть отчёта. Полный текст доступен при открытии файла."
                summary = path.with_suffix(".summary.json")
                metadata = summary if summary.is_file() else path.with_suffix(".json")
                state = json.loads(metadata.read_text(encoding="utf-8")).get("status") if metadata.is_file() else None
                self._queue.put(_ReportLoaded(request, path, content, state))
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                self._queue.put(_ReportLoaded(request, path, error=str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_report(self, event):
        if event.request != self._report_request:
            return
        self._report_loading = False
        if event.error is not None:
            self.report_status.configure(text=f"Не удалось открыть отчёт: {event.error}", text_color="#ef7777")
            return
        path, content, state = event.path, event.content, event.state
        self.last_report = path
        self.report_path_var.set(str(path))
        problem = state in {"failed", "verification_failed"}
        labels = {"completed": "Проверки завершены", "completed_with_issues": "Есть замечания и ограничения",
                  "verification_failed": "Проверка выявила несоответствия", "failed": "Операция не завершена",
                  "cancelled": "Операция остановлена", "running": "Отчёт не завершён"}
        self.report_status.configure(text=labels.get(state, "Отчёт открыт") + ". Подробности и границы проверки указаны ниже.",
                                     text_color="#ef7777" if problem else ("gray10", "gray90"))
        self.report_box.configure(state="normal")
        self.report_box.delete("1.0", "end")
        self.report_box.insert("1.0", content)
        self.report_box.configure(state="disabled")
        if problem:
            self.tabs.set("Отчёты")
            self.log(f"[ПРОВЕРКА] Обнаружены ошибки. Отчёт: {path}", True)

    def open_report(self, folder=False):
        if not self.last_report:
            return
        path = self.last_report.parent if folder else self.last_report
        if sys.platform.startswith("win"):
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _build_paths(self):
        frame = ctk.CTkFrame(self._sort_page)
        frame.pack(pady=10, padx=10, fill="x")
        self.src_var = tk.StringVar(value=self._settings["src"])
        self.tgt_var = tk.StringVar(value=self._settings["tgt"])
        self.quar_var = tk.StringVar(value=self._settings["quar"])
        self._path_row(frame, "Источник (01_Raw):", self.src_var, 0)
        self._path_row(frame, "Назначение (02_Final):", self.tgt_var, 1)
        self._path_row(frame, "Карантин (03_Quarantine):", self.quar_var, 2)

    def _path_row(self, parent, label_text, var, row):
        ctk.CTkLabel(parent, text=label_text, width=180, anchor="e").grid(
            row=row, column=0, padx=5, pady=5)
        ctk.CTkEntry(parent, textvariable=var, width=480).grid(
            row=row, column=1, padx=5, pady=5)
        ctk.CTkButton(parent, text="Обзор", width=60,
                      command=lambda: self._browse(var)).grid(row=row, column=2, padx=5, pady=5)

    def _build_options(self):
        frame = ctk.CTkFrame(self._sort_page)
        frame.pack(pady=(0, 10), padx=10, fill="x")

        row1 = ctk.CTkFrame(frame, fg_color="transparent")
        row1.pack(fill="x", padx=10, pady=(8, 2))
        self.media_var = tk.IntVar(value=int(self._settings["media"]))
        self.docs_var = tk.IntVar(value=int(self._settings["docs"]))
        self.audio_var = tk.IntVar(value=int(self._settings["audio"]))
        self.other_var = tk.IntVar(value=int(self._settings["other"]))
        for text, var in (("Медиа", self.media_var), ("Документы", self.docs_var),
                          ("Аудио", self.audio_var), ("Прочее", self.other_var)):
            ctk.CTkCheckBox(row1, text=text, variable=var).pack(side="left", padx=15, pady=5)

        row2 = ctk.CTkFrame(frame, fg_color="transparent")
        row2.pack(fill="x", padx=10, pady=(2, 8))
        ctk.CTkLabel(row2, text="Архивы после распаковки:").pack(side="left", padx=(15, 8))
        current = next((k for k, v in ARCHIVE_CHOICES.items()
                        if v == self._settings["archive_action"]), next(iter(ARCHIVE_CHOICES)))
        self.archive_var = tk.StringVar(value=current)
        ctk.CTkOptionMenu(row2, values=list(ARCHIVE_CHOICES), variable=self.archive_var,
                          width=260).pack(side="left", padx=5)
        row3 = ctk.CTkFrame(frame, fg_color="transparent")
        row3.pack(fill="x", padx=10, pady=(0, 8))
        self.dry_var = tk.IntVar(value=int(self._settings["dry_run"]))
        ctk.CTkCheckBox(row3, text="Пробный запуск (ничего не перемещать)",
                        variable=self.dry_var).pack(side="left", padx=15)

    def _build_logs(self):
        tabs = ctk.CTkTabview(self._sort_page)
        tabs.pack(pady=(0, 10), padx=10, fill="both", expand=True)
        tabs.add("Логи")
        tabs.add("Ошибки")
        self.log_box = ctk.CTkTextbox(tabs.tab("Логи"), state="disabled", font=("Consolas", 12))
        self.log_box.pack(fill="both", expand=True)
        self.error_box = ctk.CTkTextbox(tabs.tab("Ошибки"), state="disabled",
                                        font=("Consolas", 12), text_color="red")
        self.error_box.pack(fill="both", expand=True)

        self.progress = ctk.CTkProgressBar(self._sort_page)
        self.progress.pack(pady=(0, 10), padx=10, fill="x")
        self.progress.set(0)

    def _build_buttons(self):
        frame = ctk.CTkFrame(self._sort_page)
        frame.pack(pady=(0, 10), padx=10, fill="x")
        self.start_btn = ctk.CTkButton(frame, text="Запустить", height=40,
                                       command=self.start_processing)
        self.start_btn.pack(side="left", padx=10, pady=10)
        self.stop_btn = ctk.CTkButton(frame, text="Стоп", height=40, width=80, state="disabled",
                                      fg_color="#a93226", hover_color="#7b241c",
                                      command=self.stop_processing)
        self.stop_btn.pack(side="left", padx=5, pady=10)
        self.undo_btn = ctk.CTkButton(frame, text="Отменить последнюю сортировку", height=40,
                                      fg_color="gray35", command=self.start_undo)
        self.undo_btn.pack(side="left", padx=5, pady=10)
        self.open_btn = ctk.CTkButton(frame, text="Открыть результат", height=40,
                                      fg_color="gray35", command=self.open_result)
        self.open_btn.pack(side="left", padx=5, pady=10)

    # ---------- логирование и прогресс (потокобезопасно) ----------

    def log(self, message: str, is_error: bool = False):
        self._queue.put((message, is_error))

    def _set_progress(self, value: float):
        self._progress = value

    def _on_worker_done(self):
        self._queue.put(_DONE)

    def _poll(self):
        logs, errors, finished = [], [], False
        for _ in range(1000):
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _DONE:
                finished = True
                continue
            if isinstance(item, _ReportReady):
                self._show_report(item.path)
                continue
            if isinstance(item, _ReportLoaded):
                self._apply_report(item)
                continue
            if isinstance(item, DuplicateEvent):
                self.duplicates_panel.handle_event(item)
                continue
            message, is_error = item
            logs.append(message)
            if is_error:
                errors.append(message)
        if logs:
            self._append(self.log_box, logs, "log")
        if errors:
            self._append(self.error_box, errors, "err")
        self.progress.set(self._progress)
        if finished:
            self._worker_done_waiting = True
        if self._worker_done_waiting and not self._report_loading:
            self._worker_done_waiting = False
            if self._finish():
                return
        self.after(100, self._poll)

    def _append(self, box, lines, key):
        box.configure(state="normal")
        box.insert("end", "\n".join(lines) + "\n")
        count = self._line_counts[key] + len(lines)
        if count > MAX_LOG_LINES:
            box.delete("1.0", f"{count - MAX_LOG_LINES + 1}.0")
            count = MAX_LOG_LINES
        self._line_counts[key] = count
        box.see("end")
        box.configure(state="disabled")

    # ---------- действия ----------

    def _browse(self, var):
        folder = filedialog.askdirectory()
        if folder:
            var.set(folder)

    def _check_dependencies(self):
        sz = get_7z_path()
        if sz:
            self.log(f"[СИСТЕМА] 7-Zip найден: {sz}")
        else:
            self.log("[ПРЕДУПРЕЖДЕНИЕ] 7-Zip не найден. Будут распакованы только .zip; "
                     ".7z и .rar потребуют установки 7-Zip (7-zip.org).", True)

    def _collect_settings(self) -> dict:
        return {
            "src": self.src_var.get(), "tgt": self.tgt_var.get(), "quar": self.quar_var.get(),
            "media": bool(self.media_var.get()), "docs": bool(self.docs_var.get()),
            "audio": bool(self.audio_var.get()), "other": bool(self.other_var.get()),
            "archive_action": ARCHIVE_CHOICES[self.archive_var.get()],
            "dry_run": bool(self.dry_var.get()),
        }

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.start_btn.configure(state="disabled" if busy else "normal")
        self.undo_btn.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")
        self.duplicates_panel.set_busy(busy)

    def start_processing(self):
        if self._busy:
            return
        paths, errors = validate_paths(self.src_var.get(), self.tgt_var.get(),
                                       self.quar_var.get())
        if errors:
            for err in errors:
                self.log(f"[ОШИБКА] {err}", True)
            messagebox.showerror("Проверьте папки", "\n".join(errors), parent=self)
            return

        settings = self._collect_settings()
        options = Options(media=settings["media"], docs=settings["docs"],
                          audio=settings["audio"], other=settings["other"],
                          archive_action=settings["archive_action"],
                          dry_run=settings["dry_run"])

        if not options.dry_run:
            text = (f"Файлы будут ПЕРЕМЕЩЕНЫ из:\n{paths[0]}\nв:\n{paths[1]}\n\n"
                    "Все перемещения записываются в журнал; последнюю сортировку "
                    "можно отменить кнопкой «Отменить последнюю сортировку».")
            if options.archive_action == "delete":
                text += ("\n\n⚠ Архивы будут УДАЛЕНЫ после успешной распаковки "
                         "(без возможности восстановления).")
            if not messagebox.askyesno("Подтверждение", text + "\n\nПродолжить?", parent=self):
                return

        self._last_run_dry = options.dry_run
        if not options.dry_run:
            save_settings(settings)
        self._cancel.clear()
        self._progress = 0.0
        self._set_busy(True)
        self.log("[СИСТЕМА] Запуск" + (" (пробный режим)..." if options.dry_run else "..."))
        run_pipeline(*paths, options, self.log, self._set_progress,
                     self._on_worker_done, self._cancel, report_cb=self._on_report_ready)

    def stop_processing(self):
        self._cancel.set()
        self.stop_btn.configure(state="disabled")
        self.log("[СИСТЕМА] Останавливаюсь после завершения текущей операции...")

    def start_undo(self):
        if self._busy:
            return
        tgt = self.tgt_var.get().strip()
        if not tgt or not Path(tgt).expanduser().is_dir():
            messagebox.showerror("Откат", "Укажите существующую папку «Назначение».", parent=self)
            return
        if not messagebox.askyesno(
                "Откат",
                "Вернуть файлы последней сортировки на прежние места?\n"
                "(Архивы останутся в папке обработанных.)", parent=self):
            return
        self._cancel.clear()
        self._set_busy(True)
        run_undo(Path(tgt).expanduser().resolve(), self.log, self._on_worker_done,
                 cancel_event=self._cancel, report_cb=self._on_report_ready)

    def open_result(self):
        tgt = self.tgt_var.get().strip()
        if not tgt or not Path(tgt).expanduser().is_dir():
            self.log("[ОШИБКА] Папка «Назначение» не существует.", True)
            return
        if sys.platform.startswith("win"):
            os.startfile(tgt)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", tgt])
        else:
            subprocess.Popen(["xdg-open", tgt])

    def _finish(self) -> bool:
        """Вызывается, когда рабочий поток завершился. True — окно закрыто."""
        self._set_busy(False)
        self.log("[СИСТЕМА] Работа завершена.")
        if self._closing:
            self._save_settings_on_close()
            self.destroy()
            return True
        return False

    def _save_settings_on_close(self):
        settings = self._collect_settings()
        if not settings["dry_run"] and self._last_run_dry is not True:
            save_settings(settings)

    def _on_close(self):
        if self._busy:
            if not messagebox.askyesno(
                    "Идёт обработка",
                    "Обработка ещё идёт. Остановить и выйти?\n"
                    "(Окно закроется после завершения текущей операции.)", parent=self):
                return
            self._closing = True
            self._cancel.set()
            self.log("[СИСТЕМА] Ожидаю завершения текущей операции перед выходом...")
            return
        self._save_settings_on_close()
        self.destroy()
