# 📖 EPUB to Audiobook Web Service (Zeabur 雲端部署版)

基於 **FastAPI + Edge-TTS (微軟神經語音) + TailwindCSS** 的現代化有聲書轉檔服務。  
完全**不需要 GPU**，低記憶體消耗（< 256MB RAM），支援手機/電腦瀏覽器隨時上傳 EPUB、在線試聽與一鍵 ZIP 打包下載。

---

## ✨ 核心特色

1. **極致中英混讀**：採用微軟神經語音核心，英文名詞、術語切換流暢無斷層。
2. **豐富男女聲線**：
   - 👑 **雲健（磁性說書大叔・低沉男聲）**：小說、故事首選
   - 👦 **雲希（陽光青年男聲）**：影視解說、科普
   - 🇹🇼 **雲哲（台灣標準自然男聲）**：親切自然口音
   - 👑 **曉曉（溫暖知性女主播）**：有聲書女聲天花板
   - 🇹🇼 **曉臻 / 曉雨（台灣甜美/知性女聲）**
   - 🇭🇰 **雲龍 / 曉曼（香港粵語男女聲）**
3. **雙引擎 EPUB 解析**：內建 `ebooklib` + 原生 `zipfile` 雙解析引擎，相容所有現代 EPUB3 書籍。
4. **雲端一鍵打包**：轉檔完畢後自動生成全書 MP3 ZIP 壓縮檔供一鍵下載。

---

## 🚀 Zeabur 一鍵部署教學（只需 3 步驟）

### 步驟 1：建立 GitHub 倉庫
將 `epub-tts-zeabur` 資料夾內的程式碼推送到您的 GitHub 倉庫（例如 `my-epub-tts`）。

### 步驟 2：登入 Zeabur 並新增服務
1. 前往 [Zeabur 控制台](https://dash.zeabur.com/)。
2. 點擊 **Create Project（建立專案）**。
3. 點擊 **Deploy New Service（部署新服務）** → 選擇 **GitHub** → 選取剛才建立的倉庫。

### 步驟 3：綁定網域名稱
1. 部署完成後（約 1 分鐘），點擊服務卡片中的 **Networking（網路）**。
2. 點擊 **Generate Domain（產生網域）**（例如 `epub-tts.zeabur.app`）或綁定自己的自訂網域。
3. 打開該網址，即可在手機或電腦上隨時享受雲端轉檔！

---

## 💻 本地測試運行

若要在本機電腦先進行預覽測試：

```bash
cd c:\Zgemini\epub-tts-zeabur
pip install -r requirements.txt
uvicorn app:app --reload --port 8080
```
瀏覽器打開 `http://localhost:8080` 即可使用！
