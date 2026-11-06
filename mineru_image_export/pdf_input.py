"""通过独立子进程调用已安装的 MinerU，不依赖引脚提取项目。"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile

from errors import ExportError


def parse_pdf(pdf: Path, parse_root: Path, *, backend: str = "hybrid-auto-engine",
              method: str = "auto", lang: str = "ch", python: str | None = None) -> Path:
    pdf = pdf.resolve()
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        raise ExportError(f"PDF 文件不存在或扩展名不正确：{pdf}")
    with pdf.open("rb") as handle:
        if b"%PDF-" not in handle.read(1024):
            raise ExportError(f"输入文件缺少 PDF 文件头：{pdf}")

    parse_root = parse_root.resolve()
    parse_root.mkdir(parents=True, exist_ok=True)
    # 结果必须持久保存，最终 JSON 的相对路径会引用其中图片，不能当临时文件删除。
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=parse_root))
    command = [python or sys.executable, "-m", "mineru.cli.client", "-p", str(pdf),
               "-o", str(run_dir), "-b", backend, "-m", method, "--lang", lang,
               "--image-analysis", "false"]
    print(f"开始 MinerU 解析：{pdf}\n解析结果目录：{run_dir}", flush=True)
    try:
        # 参数列表直接传给子进程，不使用 shell，含空格的路径也不会被拆开。
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise ExportError(f"找不到 MinerU Python 解释器：{command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise ExportError(f"MinerU 解析失败，退出码 {exc.returncode}；请查看上方日志。"
                          "请确认已激活安装 MinerU 的虚拟环境。") from exc
    return run_dir
