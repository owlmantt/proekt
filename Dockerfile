FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV HUB_PUBLIC=1 \
    HUB_DB=/data/hub.db \
    HUB_SOLANA_CLUSTER=devnet \
    HUB_SOLANA_RPC=https://api.devnet.solana.com \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import os,urllib.request; p=os.environ.get('PORT','8000'); urllib.request.urlopen('http://127.0.0.1:%s/api/health'%p, timeout=4)"

CMD ["sh", "-c", "python indexer.py && uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
