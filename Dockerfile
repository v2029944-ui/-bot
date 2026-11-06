FROM python:3.12-slim

# логи сразу, без буфера; без .pyc
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data APP_DIR=/app

# git нужен для обновления кнопкой из бота (если задан GITHUB_REPO)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

WORKDIR /app

COPY requirements.txt requirements-dev.txt requirements.lock requirements-dev.lock ./
RUN pip install --no-cache-dir pip==26.3 \
    && pip install --no-cache-dir --require-hashes -r requirements-dev.lock \
    && mkdir -p /opt && cat requirements.txt requirements.lock requirements-dev.txt requirements-dev.lock | sha256sum | cut -d' ' -f1 > /opt/requirements.sha

COPY core.py ops.py tg.py max_.py max_queue.py max_actions.py post_footer.py comparison.py spaces.py main.py updater.py entrypoint.sh ./
COPY dialog_state.py resources.py schema.py configuration.py command_context.py editor_rules.py compatibility.json ./
COPY max_tls.py ./
COPY certs/ ./certs/
COPY services/ ./services/

# база и фото лежат в /app/data — эту папку compose выносит на диск сервера
VOLUME ["/app/data"]

CMD ["sh", "/app/entrypoint.sh"]
