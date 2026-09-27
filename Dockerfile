FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# cryptg mempercepat enkripsi upload Telethon; kalau gagal dipasang, bot tetap jalan (lebih lambat).
RUN pip install --no-cache-dir cryptg || true
COPY bot.py .

CMD ["python", "bot.py"]
