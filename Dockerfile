FROM python:3.12-slim

WORKDIR /app

# 1. Устанавливаем системные зависимости
# ffmpeg - для склеивания видео+аудио
# aria2 - для ускорения загрузки (многопоточность)
# ca-certificates - для HTTPS
# curl + unzip - для установки Deno
RUN apt-get update && apt-get install -y --no-install-recommends \
  ffmpeg \
  aria2 \
  ca-certificates \
  curl unzip \
  && rm -rf /var/lib/apt/lists/* \
  && aria2c --version | head -1

# 1.1. Устанавливаем Deno (JS runtime для YouTube n-parameter challenge)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
  && deno --version

# 2. Устанавливаем Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt yt-dlp-ejs

# 3. Копируем код приложения
COPY app ./app

# 4. Настройка окружения
ENV PYTHONUNBUFFERED=1
# Порт по умолчанию (Northflank/Heroku обычно переопределяют его через ENV)
ENV PORT=8000 

# 5. Создание пользователя без root-прав для безопасности
RUN useradd -m -s /bin/bash botuser
USER botuser

# 6. Graceful shutdown: uvicorn получает SIGINT напрямую
STOPSIGNAL SIGINT

# 7. Запуск: exec заменяет sh на uvicorn (PID 1 = корректные сигналы)
CMD ["sh", "-c", "exec uvicorn app.main:api --host 0.0.0.0 --port ${PORT}"]
