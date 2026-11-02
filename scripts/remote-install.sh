#!/usr/bin/env bash
# Run only after copying this project to the GPU data disk.
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
command -v python3 >/dev/null || { echo "需要 Python 3.10 或更高版本。"; exit 1; }
command -v ffmpeg >/dev/null || { echo "请先在 GPU 环境安装 FFmpeg。"; exit 1; }
command -v ffprobe >/dev/null || { echo "请先在 GPU 环境安装 ffprobe。"; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3,10), "需要 Python 3.10 或更高版本"'
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
mkdir -p inbox data
echo "依赖已安装。请在当前运行环境设置 CUTFLOW_TOKEN，并保持服务仅监听回环地址。"
echo "GPU 连接建议使用 SSH 端口转发；不要直接把未经额外保护的服务暴露到公网。"
echo "素材目录：$PROJECT_DIR/inbox"
echo "启动命令：CUTFLOW_INBOX=\"$PROJECT_DIR/inbox\" CUTFLOW_DATA=\"$PROJECT_DIR/data\" .venv/bin/python -m uvicorn studio.server:app --host 127.0.0.1 --port 8765 --no-access-log"
