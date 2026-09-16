"""Entry point: python run.py  (or: uvicorn harness.main:app)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn

from harness.main import app

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("MODELDOCK_HOST", "127.0.0.1"),
        port=int(os.environ.get("MODELDOCK_PORT", "8787")),
    )
