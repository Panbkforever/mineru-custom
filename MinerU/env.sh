# MinerU 环境激活脚本
# 使用方式: source /root/autodl-tmp/MinerU/env.sh

export MINERU_HOME=/root/autodl-tmp/MinerU
export MINERU_MODEL_SOURCE=modelscope

# 激活虚拟环境
source "$MINERU_HOME/.venv/bin/activate"

# 将后处理模块目录加入 Python 路径（OCR 修正、列分隔线预处理等）
export PYTHONPATH=/root/autodl-tmp:${PYTHONPATH:-}

echo "MinerU 环境已就绪"
echo "  - 版本: $(python -c 'from mineru.version import __version__; print(__version__)' 2>/dev/null || echo 'unknown')"
echo "  - 模型源: modelscope"
echo "  - 使用方式: mineru -p <输入文件> -o <输出目录>"
