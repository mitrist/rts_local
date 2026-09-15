"""Запуск: из корня проекта — python -m web_ui"""

import os

import uvicorn

_DEFAULT_PORT = 8765

if __name__ == "__main__":
    port = int(os.environ.get("RTS_WEB_PORT", str(_DEFAULT_PORT)))
    host = os.environ.get("RTS_WEB_HOST", "0.0.0.0")
    uvicorn.run("web_ui.main:app", host=host, port=port, reload=False)
