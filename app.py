import os
import re
import uuid
import shutil
import asyncio
from pathlib import Path
from typing import Dict, Any, List

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
import edge_tts

from converter import extract_chapters

app = FastAPI(title="EPUB to Audiobook Web Service")

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
UPLOAD_DIR = STORAGE_DIR / "uploads"
OUTPUT_DIR = STORAGE_DIR / "outputs"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# 支援的精選聲線清單
VOICES = [
    {
        "id": "zh-CN-YunjianNeural",
        "name": "雲健（磁性說書大叔・低沉男聲）",
        "category": "男聲",
        "gender": "male",
        "badge": "👑 小說歷史首選",
        "desc": "沉穩、厚重、帶有說書人磁性魅力，最適合小說、故事與長篇讀物。"
    },
    {
        "id": "zh-CN-YunxiNeural",
        "name": "雲希（陽光青年男聲）",
        "category": "男聲",
        "gender": "male",
        "badge": "熱門影視解說",
        "desc": "清新自然、咬字清晰，廣泛用於知識科普與影視解說。"
    },
    {
        "id": "zh-TW-YunJheNeural",
        "name": "雲哲（台灣標準自然男聲）",
        "category": "男聲",
        "gender": "male",
        "badge": "🇹🇼 台灣口音",
        "desc": "標準台灣日常語調，溫和自然、親切流暢。"
    },
    {
        "id": "zh-CN-YunyangNeural",
        "name": "雲揚（專業播音新聞男聲）",
        "category": "男聲",
        "gender": "male",
        "badge": "專業播音",
        "desc": "字正腔圓、莊重沉穩，適合商業、科技與新聞類書籍。"
    },
    {
        "id": "zh-CN-XiaoxiaoNeural",
        "name": "曉曉（溫暖知性女聲）",
        "category": "女聲",
        "gender": "female",
        "badge": "👑 女聲天花板",
        "desc": "溫暖知性、富有情感起伏，各類文學作品與情感讀物首選。"
    },
    {
        "id": "zh-TW-HsiaoChenNeural",
        "name": "曉臻（台灣溫柔甜美女聲）",
        "category": "女聲",
        "gender": "female",
        "badge": "🇹🇼 台灣口音",
        "desc": "甜美親切、輕快自然，聽感舒適不疲勞。"
    },
    {
        "id": "zh-TW-HsiaoYuNeural",
        "name": "曉雨（台灣知性成熟女聲）",
        "category": "女聲",
        "gender": "female",
        "badge": "🇹🇼 台灣口音",
        "desc": "舒緩溫柔、成熟知性，適合心靈、哲學與慢讀類書籍。"
    },
    {
        "id": "zh-HK-WanLungNeural",
        "name": "雲龍（香港粵語成熟男聲）",
        "category": "粵語",
        "gender": "male",
        "badge": "🇭🇰 粵語男聲",
        "desc": "成熟穩重的標準廣東話男聲。"
    },
    {
        "id": "zh-HK-HiuMaanNeural",
        "name": "曉曼（香港粵語自然女聲）",
        "category": "粵語",
        "gender": "female",
        "badge": "🇭🇰 粵語女聲",
        "desc": "標準廣東話流利自然女聲。"
    }
]

# 記憶體中的任務狀態管理
TASKS: Dict[str, Dict[str, Any]] = {}


def _safe_name(name: str) -> str:
    safe = re.sub(r'[\\/*?:"<>|]', '_', name).strip()
    return safe[:60] or "chapter"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "voices": VOICES
    })


@app.post("/api/upload")
async def upload_epub(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".epub"):
        raise HTTPException(status_code=400, detail="請上傳 .epub 格式的電子書！")

    task_id = str(uuid.uuid4())[:8]
    save_path = UPLOAD_DIR / f"{task_id}_{file.filename}"

    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        title, chapters = extract_chapters(save_path)
    except Exception as e:
        if save_path.exists():
            save_path.unlink()
        raise HTTPException(status_code=500, detail=f"EPUB 解析失敗: {str(e)}")

    if not chapters:
        if save_path.exists():
            save_path.unlink()
        raise HTTPException(status_code=400, detail="這本 EPUB 內沒有偵測到任何章節文字內容！")

    total_chars = sum(c["char_count"] for c in chapters)

    TASKS[task_id] = {
        "task_id": task_id,
        "title": title,
        "filename": file.filename,
        "epub_path": str(save_path),
        "chapters": chapters,
        "total_chapters": len(chapters),
        "total_chars": total_chars,
        "status": "ready",
        "progress": 0,
        "current_chapter": "",
        "completed_files": [],
        "zip_url": None,
        "error": None
    }

    return {
        "task_id": task_id,
        "title": title,
        "total_chapters": len(chapters),
        "total_chars": total_chars,
        "chapters": [{"index": c["index"], "title": c["title"], "char_count": c["char_count"]} for c in chapters]
    }


async def _run_conversion(task_id: str, voice: str, pitch: str, rate: str, selected_indices: List[int]):
    task = TASKS.get(task_id)
    if not task:
        return

    task["status"] = "processing"
    task["progress"] = 0
    task["completed_files"] = []

    book_title = task["title"]
    book_out_dir = OUTPUT_DIR / f"{task_id}_{_safe_name(book_title)}"
    book_out_dir.mkdir(parents=True, exist_ok=True)

    chapters = task["chapters"]
    target_chapters = [c for c in chapters if c["index"] in selected_indices] if selected_indices else chapters
    total = len(target_chapters)

    for i, ch in enumerate(target_chapters):
        idx = ch["index"]
        ch_title = ch["title"]
        text = ch["text"]
        
        task["current_chapter"] = f"正在轉檔第 {idx} 章：{ch_title}（{len(text):,} 字）"
        mp3_filename = f"{idx:02d}_{_safe_name(ch_title)}.mp3"
        mp3_path = book_out_dir / mp3_filename

        try:
            communicate = edge_tts.Communicate(text, voice=voice, pitch=pitch, rate=rate)
            await communicate.save(str(mp3_path))

            kb = mp3_path.stat().st_size // 1024
            task["completed_files"].append({
                "index": idx,
                "title": ch_title,
                "filename": mp3_filename,
                "size_kb": kb,
                "url": f"/api/audio/{task_id}/{mp3_filename}"
            })
        except Exception as e:
            print(f"Error converting chapter {idx}: {e}")

        task["progress"] = int(((i + 1) / total) * 100)

    # 製作 ZIP 下載檔
    try:
        zip_base_name = str(OUTPUT_DIR / f"{task_id}_{_safe_name(book_title)}")
        shutil.make_archive(zip_base_name, "zip", root_dir=str(book_out_dir))
        task["zip_url"] = f"/api/download_zip/{task_id}"
    except Exception as e:
        print(f"Failed to create zip: {e}")

    task["status"] = "completed"
    task["current_chapter"] = "🎉 全部章節轉檔完成！"


@app.post("/api/convert")
async def start_conversion(
    background_tasks: BackgroundTasks,
    task_id: str = Form(...),
    voice: str = Form("zh-CN-YunjianNeural"),
    pitch: str = Form("-4Hz"),
    rate: str = Form("-3%"),
    chapters: str = Form("")  # 以逗號分隔的 index，為空表示全部
):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="找不到指定的轉檔任務")

    selected_indices = []
    if chapters.strip():
        try:
            selected_indices = [int(x.strip()) for x in chapters.split(",") if x.strip()]
        except ValueError:
            selected_indices = []

    background_tasks.add_task(_run_conversion, task_id, voice, pitch, rate, selected_indices)
    return {"status": "started", "task_id": task_id}


@app.get("/api/status/{task_id}")
async def get_task_status(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="找不到任務")
    return {
        "status": task["status"],
        "progress": task["progress"],
        "current_chapter": task["current_chapter"],
        "completed_files": task["completed_files"],
        "zip_url": task["zip_url"],
        "error": task["error"]
    }


@app.get("/api/audio/{task_id}/{filename}")
async def stream_audio(task_id: str, filename: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任務不存在")

    book_out_dir = OUTPUT_DIR / f"{task_id}_{_safe_name(task['title'])}"
    file_path = book_out_dir / filename

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="音訊檔案不存在")

    return FileResponse(
        path=str(file_path),
        media_type="audio/mpeg",
        filename=filename
    )


@app.get("/api/download_zip/{task_id}")
async def download_zip(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任務不存在")

    zip_file = OUTPUT_DIR / f"{task_id}_{_safe_name(task['title'])}.zip"
    if not zip_file.exists():
        raise HTTPException(status_code=404, detail="ZIP 壓縮檔不存在")

    return FileResponse(
        path=str(zip_file),
        media_type="application/zip",
        filename=f"{task['title']}_有聲書.zip"
    )
