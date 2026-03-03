FROM python:3.12-slim

WORKDIR /app

# 1. Устанавливаем системные зависимости
# ffmpeg - для склеивания видео+аудио
# aria2 - для ускорения загрузки (многопоточность)
# ca-certificates - для HTTPS
RUN apt-get update && apt-get install -y --no-install-recommends \
  ffmpeg \
  aria2 \
  ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# 2. Устанавливаем Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Копируем код приложения
COPY app ./app

# 4. Настройка окружения
ENV PYTHONUNBUFFERED=1
# Порт по умолчанию (Northflank/Heroku обычно переопределяют его через ENV)
ENV PORT=8000 

# 5. Создание пользователя без root-прав для безопасности
RUN useradd -m -s /bin/bash botuser
USER botuser

# 6. Запуск через sh -c для корректной подстановки переменной окружения PORT
CMD ["sh", "-c", "uvicorn app.main:api --host 0.0.0.0 --port ${PORT}"]
