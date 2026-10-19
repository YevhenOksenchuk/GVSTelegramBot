FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Europe/Kyiv
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fonts ./fonts
# Версія (git-коміт) підставляється deploy.sh; стоїть після pip install, щоб не ламати кеш залежностей
ARG BOT_VERSION=dev
ENV BOT_VERSION=$BOT_VERSION
COPY bot ./bot
CMD ["python", "-m", "bot.main"]
