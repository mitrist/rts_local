FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV RTS_PROJECT_ROOT=/app \
    RTS_WEB_HOST=0.0.0.0 \
    RTS_WEB_PORT=8765 \
    PYTHONUNBUFFERED=1

EXPOSE 8765

CMD ["python", "-m", "web_ui"]
