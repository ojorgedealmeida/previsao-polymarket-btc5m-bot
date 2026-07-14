FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BOT_STATE_PATH=/data/hedge_state.json \
    BOT_LOCK_PATH=/data/bot.lock \
    BOT_STOP_PATH=/data/STOP_BOT

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ python3-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip \
    && pip install .

CMD ["python", "-m", "arb_bot", "--bitcoin-5m", "--maker-plan", "--execute-previsao-maker", "--watch-interval", "2", "--maker-margin-pct", "15", "--max-order-usdc", "2", "--min-seconds-left", "20", "--depth", "50", "--lock-path", "/data/bot.lock", "--stop-path", "/data/STOP_BOT"]
