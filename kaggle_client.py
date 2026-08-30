import os
import re
import json
import time
import shutil
import logging
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger("kaggle_client")

# 確保 UTF-8 寫檔環境相容
import builtins
orig_open = builtins.open
def safe_open(*args, **kwargs):
    mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
    if "b" not in mode and "encoding" not in kwargs and "w" in mode:
        kwargs["encoding"] = "utf-8"
    return orig_open(*args, **kwargs)
builtins.open = safe_open

try:
    from kaggle.api.kaggle_api_extended import KaggleApi
except ImportError:
    KaggleApi = None

class KaggleBridge:
    def __init__(self):
        self.api = None
        self.username = os.environ.get("KAGGLE_USERNAME")
        self._init_api()

    def _init_api(self):
        if not KaggleApi:
            logger.warning("Kaggle package is not installed.")
            return
        try:
            self.api = KaggleApi()
            self.api.authenticate()
            if not self.username:
                self.username = self.api.get_config_value("username")
            logger.info(f"Kaggle API authenticated successfully as '{self.username}'")
        except Exception as e:
            logger.error(f"Kaggle authentication failed: {e}")

    def is_configured(self) -> bool:
        return self.api is not None and bool(self.username)

    def create_dataset(self, task_id: str, epub_path: Path, book_title: str) -> Optional[str]:
        """建立專屬 Kaggle Dataset 並上傳 EPUB"""
        if not self.is_configured():
            raise RuntimeError("Kaggle API is not configured. Please set KAGGLE_USERNAME and KAGGLE_KEY.")
            
        dataset_slug = f"task-epub-{task_id[:8]}"
        temp_dir = epub_path.parent / f"dataset_{task_id}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            # 複製電子書
            dest_epub = temp_dir / "target_book.epub"
            shutil.copy2(epub_path, dest_epub)
            
            clean_title = re.sub(r'[^\w\s-]', '', book_title)[:40].strip() or f"Book {task_id[:6]}"
            meta = {
                "title": f"epub-{task_id[:8]}",
                "id": f"{self.username}/{dataset_slug}",
                "licenses": [{"name": "CC0-1.0"}]
            }
            with open(temp_dir / "dataset-metadata.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
                
            logger.info(f"Creating Kaggle dataset: {self.username}/{dataset_slug}")
            self.api.dataset_create_new(str(temp_dir), dir_mode="zip", quiet=False)
            full_dataset_id = f"{self.username}/{dataset_slug}"
            return full_dataset_id
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    def trigger_gpu_kernel(self, task_id: str, dataset_id: str, book_title: str, pitch_shift: float = -3.5) -> Optional[str]:
        """建立並推送 GPU 轉檔 Notebook 至 Kaggle"""
        if not self.is_configured():
            raise RuntimeError("Kaggle API is not configured.")

        kernel_slug = f"epub-tts-gpu-{task_id[:8]}"
        temp_dir = Path(os.environ.get("TEMP", "/tmp")) / f"kernel_{task_id}"
        temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", book_title)[:40].strip() or "Audiobook"
            
            meta = {
                "id": f"{self.username}/{kernel_slug}",
                "title": f"epub tts {task_id[:8]}",
                "code_file": "notebook.ipynb",
                "language": "python",
                "kernel_type": "notebook",
                "is_private": "true",
                "enable_gpu": "true",
                "enable_internet": "true",
                "dataset_sources": [dataset_id],
                "competition_sources": [],
                "kernel_sources": []
            }
            with open(temp_dir / "kernel-metadata.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)

            # 產生具備 Torchaudio 與 GPU 混合加速的最新 Notebook
            nb_json = self._build_notebook_json(safe_title, dataset_id, pitch_shift)
            with open(temp_dir / "notebook.ipynb", "w", encoding="utf-8") as f:
                json.dump(nb_json, f, indent=2, ensure_ascii=False)

            logger.info(f"Pushing Kaggle Kernel: {self.username}/{kernel_slug}")
            self.api.kernels_push(str(temp_dir))
            return f"{self.username}/{kernel_slug}"
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    def get_status(self, kernel_full_slug: str) -> Dict[str, Any]:
        """查詢 Kaggle Kernel 狀態"""
        if not self.is_configured():
            return {"status": "UNKNOWN", "failure_message": "API not configured"}
        try:
            status_obj = self.api.kernels_status(kernel_full_slug)
            status_str = str(getattr(status_obj, "status", status_obj)).upper()
            fail_msg = getattr(status_obj, "failureMessage", None) or getattr(status_obj, "failure_message", None)
            
            # 標準化狀態字串
            if "COMPLETE" in status_str:
                norm_status = "COMPLETED"
            elif "RUNNING" in status_str:
                norm_status = "RUNNING"
            elif "ERROR" in status_str or "FAIL" in status_str:
                norm_status = "FAILED"
            elif "QUEUED" in status_str:
                norm_status = "QUEUED"
            else:
                norm_status = status_str
                
            return {
                "status": norm_status,
                "failure_message": fail_msg
            }
        except Exception as e:
            logger.error(f"Error querying kernel status: {e}")
            return {"status": "ERROR", "failure_message": str(e)}

    def download_output(self, kernel_full_slug: str, dest_dir: Path) -> Optional[Path]:
        """下載完成的 ZIP 檔案"""
        if not self.is_configured():
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            logger.info(f"Downloading output for {kernel_full_slug} to {dest_dir}")
            self.api.kernels_output(kernel_full_slug, str(dest_dir), file_pattern=r".*\.zip$", quiet=False)
            
            # 尋找下載的 zip 檔
            for f in dest_dir.glob("*.zip"):
                if f.stat().st_size > 1024:
                    return f
            return None
        except Exception as e:
            logger.error(f"Error downloading kernel output: {e}")
            return None

    def _build_notebook_json(self, book_title: str, dataset_id: str, pitch_shift: float) -> dict:
        """組裝經過驗證的極速轉檔 Notebook"""
        cell1 = """# 📚 Kaggle GPU 極速有聲書轉檔
!nvidia-smi
import os, sys
from pathlib import Path

!rm -rf /tmp/MeloTTS
!git clone https://github.com/myshell-ai/MeloTTS.git /tmp/MeloTTS
!pip install -q "gruut[de,es,fr]" cn2an pypinyin jieba inflect g2p_en anyascii pykakasi fugashi unidic-lite mecab-python3 jamo num2words cached_path soundfile librosa timm einops transformers ebooklib pydub beautifulsoup4 torchaudio

import nltk
nltk.download('averaged_perceptron_tagger_eng', quiet=True)
nltk.download('averaged_perceptron_tagger', quiet=True)
nltk.download('cmudict', quiet=True)

if "/tmp/MeloTTS" not in sys.path:
    sys.path.insert(0, "/tmp/MeloTTS")

import torch, torchaudio
torch.set_num_threads(4)
from melo.api import TTS
print("🎉 MeloTTS 與環境就緒！")"""

        cell2 = """# 核心解析與 C++ 聲學變調加速
import os, re, gc, time, zipfile, shutil, traceback
import numpy as np
import xml.etree.ElementTree as ET
from pathlib import Path
import soundfile as sf
import torch, torchaudio
import torchaudio.functional as F_audio
from pydub import AudioSegment
from bs4 import BeautifulSoup

WORKING_DIR = Path("/kaggle/working")
TMP_DIR = Path("/tmp/epub_work")
TMP_DIR.mkdir(parents=True, exist_ok=True)

def extract_chapters(epub_path: Path) -> tuple[str, list[dict]]:
    book_title = "__BOOK_TITLE__"
    with zipfile.ZipFile(epub_path, "r") as z:
        rootfile = "content.opf"
        try:
            c_xml = z.read("META-INF/container.xml")
            c_root = ET.fromstring(c_xml)
            for elem in c_root.iter():
                if elem.tag.endswith("rootfile"):
                    rootfile = elem.attrib.get("full-path", rootfile)
                    break
        except Exception:
            pass
        
        opf_dir = str(Path(rootfile).parent).replace("\\", "/")
        if opf_dir == ".": opf_dir = ""
        opf_xml = z.read(rootfile)
        opf_root = ET.fromstring(opf_xml)
        
        for elem in opf_root.iter():
            if elem.tag.endswith("title") and elem.text:
                book_title = elem.text.strip()
                break
                
        manifest = {}
        for elem in opf_root.iter():
            if elem.tag.endswith("item"):
                i_id = elem.attrib.get("id")
                href = elem.attrib.get("href")
                if i_id and href:
                    manifest[i_id] = (opf_dir + "/" + href).lstrip("/") if opf_dir else href
                    
        spine = []
        for elem in opf_root.iter():
            if elem.tag.endswith("itemref"):
                idref = elem.attrib.get("idref")
                if idref in manifest:
                    spine.append(manifest[idref])
                    
        if not spine:
            spine = [v for k, v in manifest.items() if v.lower().endswith((".xhtml", ".html", ".htm"))]
            
        chapters, n = [], 0
        for item in spine:
            try:
                raw = z.read(item).decode("utf-8", errors="replace")
                soup = BeautifulSoup(raw, "html.parser")
                text = soup.get_text("\n\n", strip=True)
                text = re.sub(r"[ \t]+", " ", text)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()
                if len(text) < 50:
                    continue
                n += 1
                h = soup.find(["h1", "h2", "h3"])
                title = h.get_text(" ", strip=True) if h else f"第 {n} 章"
                chapters.append({"index": n, "title": title, "text": text})
            except Exception:
                pass
        return book_title, chapters

def chunk_text(text: str, max_chars: int = 250) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks, cur, cur_len = [], [], 0
    for p in paras:
        if cur_len + len(p) > max_chars and cur:
            chunks.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(p)
        cur_len += len(p)
    if cur:
        chunks.append("\n\n".join(cur))
    return [c for c in chunks if re.search(r"[\w\u4e00-\u9fa5]", c)]

def apply_deep_male_filter(wav_path: Path, out_path: Path, n_steps: float = __PITCH_SHIFT__) -> None:
    try:
        y, sr = sf.read(str(wav_path))
        if y.ndim > 1: y = y[:, 0]
        tensor_wave = torch.from_numpy(y).float().unsqueeze(0)
        shifted = F_audio.pitch_shift(tensor_wave, sr, n_steps=n_steps)
        sf.write(str(out_path), shifted.squeeze(0).numpy(), sr, subtype='PCM_16')
    except Exception:
        shutil.copy2(wav_path, out_path)
""".replace("__BOOK_TITLE__", book_title).replace("__PITCH_SHIFT__", str(pitch_shift))

        cell3 = """# 執行批次轉檔與輸出封裝
device = "cpu"
if torch.cuda.is_available():
    major, _ = torch.cuda.get_device_capability()
    device = "cuda" if major >= 7 else "cpu"
print(f"🚀 Active device: {device}")

input_candidates = list(Path("/kaggle/input").rglob("*.epub"))
target_epub = input_candidates[0] if input_candidates else None

if not target_epub:
    print("❌ No EPUB found in dataset")
else:
    book_title, chapters = extract_chapters(target_epub)
    print(f"📚 書名：《{book_title}》，共 {len(chapters)} 章")
    
    tts_model = TTS(language="ZH", device=device)
    speaker_id = next(iter(tts_model.hps.data.spk2id.values()))
    
    safe_title = re.sub(r'[\\/*?:"<>|]', "_", book_title)[:40].strip() or "Audiobook"
    out_book_dir = WORKING_DIR / "audiobooks" / safe_title
    out_book_dir.mkdir(parents=True, exist_ok=True)
    
    silence = AudioSegment.silent(duration=350)
    for ch in chapters:
        idx, title, text = ch["index"], ch["title"], ch["text"]
        safe_ch = re.sub(r'[\\/*?:"<>|]', "_", title)[:40]
        out_mp3 = out_book_dir / f"{idx:02d}_{safe_ch}.mp3"
        if out_mp3.exists() and out_mp3.stat().st_size > 10240:
            continue
            
        print(f"[{idx:02d}/{len(chapters):02d}] 正在處理：{title} ({len(text)} 字)...")
        chunks = chunk_text(text, max_chars=250)
        segments = []
        try:
            for chunk in chunks:
                raw_wav = TMP_DIR / "raw.wav"
                pitch_wav = TMP_DIR / "pitch.wav"
                tts_model.tts_to_file(chunk, speaker_id, str(raw_wav), speed=0.95, quiet=True)
                if raw_wav.exists() and raw_wav.stat().st_size > 0:
                    apply_deep_male_filter(raw_wav, pitch_wav, n_steps=__PITCH_SHIFT__)
                    target = pitch_wav if pitch_wav.exists() else raw_wav
                    segments.append(AudioSegment.from_file(str(target)))
            if segments:
                combined = segments[0]
                for seg in segments[1:]: combined += silence + seg
                combined.export(str(out_mp3), format="mp3", bitrate="128k")
                print(f"   ✅ 完成：{out_mp3.name}")
        except Exception as e:
            print(f"   ❌ 錯誤：{e}")
            
        if idx % 4 == 0:
            gc.collect()
            if torch.cuda.is_available(): torch.cuda.empty_cache()
            
    zip_path = WORKING_DIR / f"{safe_title}_有聲書MP3"
    shutil.make_archive(str(zip_path), "zip", str(out_book_dir))
    print(f"📦 轉檔完成！ZIP 已產出：{zip_path}.zip")
""".replace("__PITCH_SHIFT__", str(pitch_shift))

        def to_lines(t): return t.splitlines(keepends=True)
        return {
            "cells": [
                {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": to_lines(cell1)},
                {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": to_lines(cell2)},
                {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": to_lines(cell3)},
            ],
            "metadata": {
                "accelerator": "GPU",
                "kaggle": {
                    "accelerator": "nvidiaTeslaT4",
                    "dataSources": [{"datasetId": dataset_id, "sourceType": "dataset"}],
                    "dockerImageVersionId": 30664,
                    "isGpuEnabled": True,
                    "isInternetEnabled": True,
                    "language": "python",
                    "sourceType": "notebook"
                },
                "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}
            },
            "nbformat": 4,
            "nbformat_minor": 4
        }
