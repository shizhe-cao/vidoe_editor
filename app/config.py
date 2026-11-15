import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
WEB_DIR = BASE_DIR / "web"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 阿里云百炼模型
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
PLANNER_MODEL = os.environ.get("PLANNER_MODEL", "qwen-plus")

# 默认输出规格
DEFAULT_RESOLUTION = (1920, 1080)
DEFAULT_FPS = 30
