FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt
RUN python -c "from yt_dlp_plugins.extractor.threads import ThreadsIE; print(ThreadsIE.IE_NAME)"

COPY bot.py .

CMD ["python", "bot.py"]
