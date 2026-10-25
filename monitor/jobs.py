"""Фоновая загрузка Excel (чтобы интерфейс не «висел» и показывал прогресс)
и диалог выбора файла."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, db, importer


def remember_file(conn, path: str, workbook: dict | None = None) -> None:
    s = db.load_settings(conn)
    s["source_file"] = path
    rec = [p for p in s.get("recent_files", []) if p != path]
    s["recent_files"] = [path] + rec[:7]
    db.save_settings(conn, s)
    if workbook:
        db.set_meta(conn, "workbook_info", {"file": path, **workbook})


class ImportJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"running": False, "stage": "", "detail": "", "pct": 0, "result": None,
                      "started_at": None, "path": "", "seq": 0}

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def start(self, path: str, force: bool = False, mode: str = "excel") -> bool:
        """mode: excel — файл выгрузки; api — напрямую с портала."""
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(running=True, stage="Подготовка", detail="", pct=0, result=None,
                              started_at=time.time(), path=path, mode=mode, seq=self.state["seq"] + 1)
        t = threading.Thread(target=self._run, args=(path, force, mode), daemon=True)
        t.start()
        return True

    def _progress(self, stage, detail, pct):
        with self.lock:
            self.state["stage"] = stage
            self.state["detail"] = detail
            if pct is not None:
                self.state["pct"] = round(pct, 1)

    def _run(self, path: str, force: bool, mode: str = "excel"):
        result = None
        conn = None
        try:
            conn = db.connect()
            settings = db.load_settings(conn)
            has_data = conn.execute("SELECT 1 FROM repairs LIMIT 1").fetchone()
            if has_data:
                self._progress("Резервная копия базы", "", 1)
                db.backup(conn, "before_import", keep=int(settings.get("backup_keep") or 15))
            if mode == "api":
                result = importer.run_api_import(conn, settings, progress=self._progress, force=force)
                if result["status"] == "ok":
                    db.set_meta(conn, "workbook_info", {"file": "Портал (API)",
                                                        **((result.get("report") or {}).get("workbook") or {})})
            else:
                result = importer.run_import(conn, path, settings, progress=self._progress, force=force)
                if result["status"] in ("ok", "duplicate"):
                    remember_file(conn, path, (result.get("report") or {}).get("workbook"))
        except Exception as e:  # noqa: BLE001
            result = {"status": "error", "message": f"{e.__class__.__name__}: {e}"}
        finally:
            if conn is not None:
                conn.close()
            if result and "report" in result:
                result = {**result, "report": {k: v for k, v in result["report"].items() if k != "workbook"}}
            with self.lock:
                self.state.update(running=False, result=result, pct=100 if result and result.get("status") == "ok"
                                  else self.state["pct"])
            if result and result.get("status") == "ok":
                _start_act_checks()


def _start_act_checks() -> None:
    """После загрузки — проверка актов АНРП/АТО через API трекинга (если включена)."""
    from . import tracking_api
    try:
        conn = db.connect()
        try:
            cfg = tracking_api.settings_cfg(db.load_settings(conn))
        finally:
            conn.close()
        if cfg.get("enabled") and cfg.get("auto"):
            tracking_api.JOB.start()
    except Exception:  # noqa: BLE001 — проверка актов не должна мешать загрузке
        pass


JOB = ImportJob()


# ------------------------------------------------------------ выбор файла
def pick_file_subprocess(initial: str = "") -> tuple[str | None, str | None]:
    """Открывает стандартный диалог Windows в отдельном процессе (так надёжнее,
    чем из потока веб-сервера). Возвращает (путь, ошибка)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--pick-file", initial or ""]
    else:
        cmd = [sys.executable, str(config.app_dir() / "app.py"), "--pick-file", initial or ""]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=600, env=env,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return None, "Окно выбора файла закрыто по таймауту."
    except OSError as e:
        return None, f"Не удалось открыть окно выбора файла: {e}"
    out = cp.stdout.decode("utf-8", "replace").strip()
    err = cp.stderr.decode("utf-8", "replace").strip()
    if cp.returncode == 3:
        return None, ("Окно выбора файла недоступно (нет модуля tkinter). "
                      "Вставьте путь к файлу в поле вручную.")
    if cp.returncode != 0:
        return None, f"Ошибка окна выбора файла: {err[-300:] or cp.returncode}"
    return (out or None), None


def run_file_dialog(initial: str = "") -> int:
    """Режим `app.py --pick-file`: печатает выбранный путь и завершается."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return 3
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:  # noqa: BLE001
        pass
    init_dir = str(Path(initial).parent) if initial and Path(initial).parent.exists() else None
    path = filedialog.askopenfilename(
        parent=root,
        title="Выберите выгрузку ремонтов (Excel)",
        initialdir=init_dir,
        filetypes=[("Книги Excel", "*.xlsx *.xlsm"), ("Все файлы", "*.*")],
    )
    root.destroy()
    if path:
        sys.stdout.write(str(Path(path)))
        sys.stdout.flush()
    return 0
