r"""啟動 VCOD:Web UI 埠取自設定(config.json "web_port",預設 8082)。

用法:.\.venv\Scripts\python.exe run.py
"""
import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.web_port)
