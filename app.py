import os
import re
import uuid
import shutil
import asyncio
import logging
import traceback
from pathlib import Path
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
import edge_tts

from converter import extract_chapters
from kaggle_client import KaggleBridge
from scheduler import TaskManager, PollingScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("app")

app = FastAPI(title="EPUB to Audiobook Web Service (Zeabur & Kaggle GPU)")

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
UPLOAD_DIR = STORAGE_DIR / "uploads"
OUTPUT_DIR = STORAGE_DIR / "outputs"
SAMPLE_DIR = STORAGE_DIR / "samples"
TEMP_DIR = STORAGE_DIR / "temp"

for d in (UPLOAD_DIR, OUTPUT_DIR, SAMPLE_DIR, TEMP_DIR):
    d.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# 初始化模組
kaggle_bridge = KaggleBridge()
task_manager = TaskManager(STORAGE_DIR)
scheduler = PollingScheduler(task_manager, kaggle_bridge)

@app.on_event("startup")
def on_startup():
    scheduler.start()

@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown()

# 支援的聲線清單
VOICES = [
    {
        "id": "melo_deep_male",
        "name": "MeloTTS 磁性說書人（⚡ Kaggle GPU 混合硬體加速）",
        "engine": "kaggle_gpu",
        "category": "深度學習男聲",
        "badge": "⚡ Kaggle GPU 極速",
        "desc": "採用 MeloTTS 神經網路 + C++ Torchaudio 磁性調音，全書 3~5 分鐘極速生成。"
    },
    {
        "id": "zh-CN-YunjianNeural",
        "name": "雲健（磁性說書大叔・低沉男聲）",
        "engine": "edge_tts",
        "category": "Edge-TTS 男聲",
        "badge": "👑 小說歷史首選",
        "desc": "沉穩、厚重、帶有說書人磁性魅力，最適合長篇小說與歷史故事。"
    },
    {
        "id": "zh-CN-YunxiNeural",
        "name": "雲希（陽光青年男聲）",
        "engine": "edge_tts",
        "category": "Edge-TTS 男聲",
        "badge": "熱門影視解說",
        "desc": "清新自然、咬字清晰，廣泛用於知識科普與影視解說。"
    },
    {
        "id": "zh-TW-YunJheNeural",
        "name": "雲哲（台灣標準自然男聲）",
        "engine": "edge_tts",
        "category": "Edge-TTS 男聲",
        "badge": "🇹🇼 台灣口音",
        "desc": "標準台灣日常語調，溫和自然、親切流暢。"
    },
    {
        "id": "zh-CN-XiaoxiaoNeural",
        "name": "曉曉（知性知心女聲）",
        "engine": "edge_tts",
        "category": "Edge-TTS 女聲",
        "badge": "情感散文首選",
        "desc": "溫暖細膩、富含情感，適合散文、心靈勵志與感性故事。"
    }
]

# ==========================================
# 網頁路由 (Web Pages)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    """首頁：EPUB 上傳與轉檔設定"""
    kaggle_ready = kaggle_bridge.is_configured()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "voices": VOICES,
            "kaggle_ready": kaggle_ready,
            "kaggle_user": kaggle_bridge.username or "未設定"
        }
    )

@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    """任務狀態查詢與進度看板面板"""
    tasks = task_manager.list_tasks()
    return templates.TemplateResponse(
        request=request,
        name="tasks.html",
        context={
            "tasks": tasks,
            "kaggle_ready": kaggle_bridge.is_configured()
        }
    )

# ==========================================
# API 端點 (REST Endpoints)
# ==========================================
@app.post("/api/tasks/submit")
async def submit_task(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    voice_id: str = Form("melo_deep_male"),
    rate: str = Form("+0%"),
    volume: str = Form("+0%")
):
    """上傳 EPUB 並建立轉檔任務"""
    if not file.filename.lower().endswith(".epub"):
        raise HTTPException(status_code=400, detail="請上傳標準 .epub 格式電子書檔案")

    task_id = uuid.uuid4().hex[:8]
    safe_filename = re.sub(r'[\\/*?:"<>|]', "_", file.filename)
    epub_path = UPLOAD_DIR / f"{task_id}_{safe_filename}"

    with open(epub_path, "wb") as f:
        content = await file.read()
        f.write(content)

    # 解析章節與書名
    try:
        book_title, chapters = extract_chapters(epub_path)
    except Exception as e:
        book_title = Path(safe_filename).stem
        chapters = []

    selected_voice = next((v for v in VOICES if v["id"] == voice_id), VOICES[0])
    engine = selected_voice.get("engine", "edge_tts")

    # 建立任務記錄
    task = task_manager.create_task(
        task_id=task_id,
        book_title=book_title,
        engine=engine,
        total_chapters=len(chapters)
    )

    # 派發至背景執行工作
    if engine == "kaggle_gpu":
        if not kaggle_bridge.is_configured():
            task_manager.update_task(
                task_id,
                status="FAILED",
                progress_message="Zeabur 環境變數未配置 KAGGLE_USERNAME / KAGGLE_KEY，無法呼叫 Kaggle GPU 服務。"
            )
            return JSONResponse({"status": "error", "message": "Kaggle API 尚未設定", "task_id": task_id})
        background_tasks.add_task(_process_kaggle_submission, task_id, epub_path, book_title)
    else:
        # 本地 Edge-TTS 輕量轉檔
        background_tasks.add_task(_process_edge_tts_conversion, task_id, epub_path, book_title, chapters, voice_id, rate, volume)

    return JSONResponse({
        "status": "success",
        "task_id": task_id,
        "book_title": book_title,
        "total_chapters": len(chapters),
        "redirect_url": "/tasks"
    })

@app.get("/api/tasks")
async def get_all_tasks():
    """取得所有任務清單（含上次更新時間、狀態、進度%）"""
    return JSONResponse(task_manager.list_tasks())

@app.get("/api/tasks/{task_id}")
async def get_task_details(task_id: str):
    """查詢單一任務詳細狀態"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="找不到指定的任務")
    return JSONResponse(task)

@app.post("/api/tasks/{task_id}/refresh")
async def refresh_task_status(task_id: str):
    """使用者點擊「手動立即檢查狀態」"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="找不到指定的任務")
    
    # 觸發單次檢查並更新上次更新時間
    updated_task = scheduler.check_task(task_id)
    return JSONResponse(updated_task)

@app.get("/api/download/{task_id}")
async def download_audiobook(task_id: str):
    """一鍵下載已完成的有聲書 ZIP 檔案"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="找不到該任務")

    # 尋找匹配的 zip 檔案
    zip_candidates = list(OUTPUT_DIR.glob(f"{task_id}*.zip"))
    if not zip_candidates:
        # 搜尋子資料夾中的 zip
        zip_candidates = list((OUTPUT_DIR / task_id).glob("*.zip"))

    if not zip_candidates or not zip_candidates[0].exists():
        raise HTTPException(status_code=404, detail="檔案尚未生成或正在處理中")

    target_zip = zip_candidates[0]
    safe_title = re.sub(r'[\\/*?:"<>|]', "_", task["book_title"])
    return FileResponse(
        path=str(target_zip),
        filename=f"{safe_title}_有聲書MP3.zip",
        media_type="application/zip"
    )

# ==========================================
# 背景協調處理邏輯 (Background Workers)
# ==========================================
def _process_kaggle_submission(task_id: str, epub_path: Path, book_title: str):
    """背景處理：建立 Kaggle Dataset 與推送 GPU Kernel"""
    try:
        task_manager.update_task(
            task_id,
            status="DATASET_UPLOADING",
            progress_percent=10,
            progress_message="正在上傳電子書至 Kaggle 私有資料集環境..."
        )
        dataset_id = kaggle_bridge.create_dataset(task_id, epub_path, book_title)
        
        task_manager.update_task(
            task_id,
            status="RUNNING",
            dataset_slug=dataset_id,
            progress_percent=20,
            progress_message="資料集已掛載！正在啟動 Kaggle GPU 運算核心..."
        )
        kernel_slug = kaggle_bridge.trigger_gpu_kernel(task_id, dataset_id, book_title)
        
        task_manager.update_task(
            task_id,
            status="RUNNING",
            kernel_slug=kernel_slug,
            progress_percent=25,
            progress_message="Kaggle GPU 算力已成功調度，每 10 分鐘自動同步最新進度..."
        )
    except Exception as e:
        logger.error(f"Failed in Kaggle submission for {task_id}: {e}\n{traceback.format_exc()}")
        task_manager.update_task(
            task_id,
            status="FAILED",
            progress_percent=0,
            progress_message=f"Kaggle 排程發送失敗：{str(e)}",
            error=str(e)
        )

async def _process_edge_tts_conversion(task_id: str, epub_path: Path, book_title: str, chapters: list, voice_id: str, rate: str, volume: str):
    """背景處理：本機 Edge-TTS 輕量轉檔"""
    try:
        out_book_dir = OUTPUT_DIR / task_id / book_title
        out_book_dir.mkdir(parents=True, exist_ok=True)
        total = len(chapters)
        
        for idx, ch in enumerate(chapters, 1):
            pct = int((idx / total) * 90)
            task_manager.update_task(
                task_id,
                status="RUNNING",
                completed_chapters=idx - 1,
                progress_percent=pct,
                progress_message=f"正在轉檔第 {idx}/{total} 章：{ch['title']}..."
            )
            
            safe_ch_title = re.sub(r'[\\/*?:"<>|]', "_", ch["title"])[:35]
            mp3_out = out_book_dir / f"{idx:02d}_{safe_ch_title}.mp3"
            
            communicate = edge_tts.Communicate(ch["text"], voice_id, rate=rate, volume=volume)
            await communicate.save(str(mp3_out))
            
        zip_path = OUTPUT_DIR / f"{task_id}_{book_title}"
        shutil.make_archive(str(zip_path), "zip", str(out_book_dir))
        final_zip = Path(f"{zip_path}.zip")
        size_mb = round(final_zip.stat().st_size / (1024 * 1024), 2)
        
        task_manager.update_task(
            task_id,
            status="COMPLETED",
            completed_chapters=total,
            progress_percent=100,
            progress_message="轉檔已全數完成！隨時可點擊下載完整有聲書 MP3 壓縮檔。",
            download_url=f"/api/download/{task_id}",
            file_size_mb=size_mb
        )
    except Exception as e:
        logger.error(f"Edge-TTS failed for {task_id}: {e}\n{traceback.format_exc()}")
        task_manager.update_task(
            task_id,
            status="FAILED",
            progress_message=f"轉檔發生異常：{str(e)}",
            error=str(e)
        )
