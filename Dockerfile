FROM python:3.10-slim

WORKDIR /app

# 安裝 ffmpeg（音訊處理必要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 建立輸出與暫存目錄
RUN mkdir -p /app/storage/uploads /app/storage/outputs

ENV PORT=8080
EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
