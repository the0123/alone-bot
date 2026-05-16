FROM python:3.12-slim

# Don't write .pyc files, don't buffer stdout (so logs appear immediately)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first, separately from app code — this layer gets cached
# and only rebuilds when requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then copy the app
COPY alone_bot/ ./alone_bot/
COPY config.toml .

# Run the bot
CMD ["python", "-m", "alone_bot.main"]