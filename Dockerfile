FROM python:3.12-slim

# логи сразу, без буфера; без .pyc
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data APP_DIR=/app

# git нужен для обновления кнопкой из бота (если задан GITHUB_REPO)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt \
    && mkdir -p /opt && sha256sum requirements.txt | cut -d' ' -f1 > /opt/requirements.sha

COPY core.py ops.py tg.py max_.py max_queue.py max_actions.py post_footer.py main.py updater.py entrypoint.sh ./

# база и фото лежат в /app/data — эту папку compose выносит на диск сервера
VOLUME ["/app/data"]

CMD ["sh", "/app/entrypoint.sh"]
