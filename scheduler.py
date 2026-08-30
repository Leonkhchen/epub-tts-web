import os
import json
import time
import datetime
import threading
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

from kaggle_client import KaggleBridge

logger = logging.getLogger("scheduler")

class TaskManager:
    def __init__(self, storage_dir: Path):
        self.storage_dir = storage_dir
        self.db_file = storage_dir / "tasks.json"
        self.output_dir = storage_dir / "outputs"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self.db_file.exists():
            try:
                with open(self.db_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load tasks.json: {e}")
        return {}

    def _save(self):
        try:
            with open(self.db_file, "w", encoding="utf-8") as f:
                json.dump(self._tasks, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save tasks.json: {e}")

    def create_task(self, task_id: str, book_title: str, engine: str, total_chapters: int = 0) -> dict:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        task = {
            "task_id": task_id,
            "book_title": book_title,
            "engine": engine,
            "created_at": now_str,
            "last_updated_at": now_str,
            "status": "QUEUED",
            "progress_percent": 0,
            "progress_message": "任務已建立，準備送出至雲端運算環境...",
            "total_chapters": total_chapters,
            "completed_chapters": 0,
            "kernel_slug": None,
            "dataset_slug": None,
            "download_url": None,
            "file_size_mb": 0.0,
            "error": None
        }
        self._tasks[task_id] = task
        self._save()
        return task

    def update_task(self, task_id: str, **kwargs) -> Optional[dict]:
        if task_id not in self._tasks:
            return None
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        kwargs["last_updated_at"] = now_str
        self._tasks[task_id].update(kwargs)
        self._save()
        return self._tasks[task_id]

    def get_task(self, task_id: str) -> Optional[dict]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> List[dict]:
        return sorted(list(self._tasks.values()), key=lambda x: x.get("created_at", ""), reverse=True)


class PollingScheduler:
    def __init__(self, task_manager: TaskManager, kaggle_bridge: KaggleBridge):
        self.tm = task_manager
        self.kaggle = kaggle_bridge
        self.interval_seconds = 600  # 10 分鐘
        self._running = False
        self._thread = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()
        logger.info(f"Started 10-minute periodic polling scheduler (Interval: {self.interval_seconds // 60}m)")

    def shutdown(self):
        self._running = False

    def _worker_loop(self):
        while self._running:
            # 依設定間隔等待（預設 600 秒）
            for _ in range(self.interval_seconds):
                if not self._running:
                    return
                time.sleep(1)
            try:
                self.check_all_running_tasks()
            except Exception as e:
                logger.error(f"Error in scheduler worker loop: {e}")

    def check_task(self, task_id: str) -> Optional[dict]:
        """單一任務狀態檢查與結果下載邏輯"""
        task = self.tm.get_task(task_id)
        if not task:
            return None

        kernel_slug = task.get("kernel_slug")
        if not kernel_slug:
            return task

        # 如果已經完成或失敗，直接回傳
        if task.get("status") in ("COMPLETED", "FAILED"):
            return task

        logger.info(f"Checking status for task {task_id} ({kernel_slug})...")
        res = self.kaggle.get_status(kernel_slug)
        status = res.get("status", "UNKNOWN")
        fail_msg = res.get("failure_message")

        if status == "COMPLETED":
            logger.info(f"Task {task_id} is COMPLETED! Downloading results...")
            dest_dir = self.tm.output_dir / task_id
            zip_file = self.kaggle.download_output(kernel_slug, dest_dir)
            
            size_mb = 0.0
            if zip_file and zip_file.exists():
                size_mb = round(zip_file.stat().st_size / (1024 * 1024), 2)
                clean_zip = self.tm.output_dir / f"{task_id}_{task['book_title']}.zip"
                zip_file.rename(clean_zip)
                download_url = f"/api/download/{task_id}"
            else:
                download_url = f"/api/download/{task_id}"

            task = self.tm.update_task(
                task_id,
                status="COMPLETED",
                progress_percent=100,
                progress_message="轉檔已全數完成！隨時可點擊下載完整有聲書 MP3 壓縮檔。",
                download_url=download_url,
                file_size_mb=size_mb
            )
        elif status == "RUNNING":
            cur_p = task.get("progress_percent", 10)
            new_p = min(95, cur_p + 15)
            task = self.tm.update_task(
                task_id,
                status="RUNNING",
                progress_percent=new_p,
                progress_message=f"Kaggle GPU 正在全速進行神經網路語音合成中... (預估進度 {new_p}%)"
            )
        elif status == "FAILED":
            task = self.tm.update_task(
                task_id,
                status="FAILED",
                progress_percent=0,
                progress_message=f"轉檔發生錯誤：{fail_msg or '未知異常'}",
                error=fail_msg
            )
        else:
            task = self.tm.update_task(
                task_id,
                status=status,
                progress_message=f"當前狀態：{status}..."
            )
            
        return task

    def check_all_running_tasks(self):
        """定期 10 分鐘巡檢所有執行中的任務"""
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"[{now_str}] Running 10-minute periodic check for all active tasks...")
        tasks = self.tm.list_tasks()
        for t in tasks:
            if t.get("status") in ("QUEUED", "RUNNING"):
                try:
                    self.check_task(t["task_id"])
                except Exception as e:
                    logger.error(f"Error checking task {t['task_id']}: {e}")
