# MinerU 图片信息导出工具

这是独立工具，不依赖或调用引脚提取项目。
主流程是：输入 PDF → 调用已安装的 MinerU 解析 → 导出图片、图注和完整章节 JSON。
也兼容读取已有 `content_list.json` 或 `content_list_v2.json`，复用解析结果时不再调用 MinerU。
工具自身只使用 Python 3.10+ 标准库。PDF 解析需要已安装、配置好模型的 MinerU 环境，
并会使用 GPU/本地解析模型；本工具不调用引脚语义分类或 DeepSeek 等 API。

## 用法

```bash
cd /root/autodl-tmp
source MinerU/.venv/bin/activate

python mineru_image_export/export_images.py \
  -i /root/autodl-tmp/pdf_Elan/EM78P134N_10.pdf \
  -o /root/autodl-tmp/mineru_image_export/exports/EM78P134N_10_images.json
```

默认输出到本目录 `exports/<文档名>_images.json`。PDF 解析产物和图片保存在
`parsed/run-随机标识/` 下，每次运行独立保存，既有解析目录不会被覆盖或误用。
最终 JSON 的图片相对路径会指向本次解析产物，请勿删除对应目录。

解析默认采用 `hybrid-auto-engine`、`auto`、`ch`，可以使用以下参数调整：

```bash
python export_images.py -i /path/document.pdf -b pipeline -m auto --lang en \
  --parse-output-dir /path/independent_parse_results
```

默认使用当前 Python 运行 `-m mineru.cli.client`。如果未激活 MinerU 环境，
可以传 `--mineru-python /root/autodl-tmp/MinerU/.venv/bin/python`。
子进程日志直接显示在终端，失败时停止导出，不会回退读取旧结果。
默认关闭额外图片内容分析，保留图片裁切、图注和章节提取。

只想复用已有解析结果时：

```bash
python export_images.py -i /path/document_content_list.json -o /path/document_images.json
```

输出已存在时默认在 PDF 解析启动前拒绝覆盖，确需重新解析并覆盖 JSON 时加 `--overwrite`。
解析失败时保留原 JSON；历史解析产物保留，不自动删除。
目录输入一次只接受一个 PDF 的解析结果；同时存在 v1/v2 时只读 v1。
也可以显式传入 v2 文件，结果不会与 v1 重复合并。

## 输出

每个图片条目一条记录，仅包含以下三个字段：

```json
[
  {
    "image_path": "../parsed/run-example/document/hybrid_auto/images/example.jpg",
    "image_title": "Figure 2 Pin Assignment",
    "section": "2 Interface Description > 2.1 Pin Diagram"
  }
]
```

- 路径相对于输出 JSON 所在目录，指向本次或复用的解析图片。保留 JPG/PNG 等原格式，不额外转码、复制图片。
- 只提取 MinerU 标记为 `image` 的条目，不包含表格截图、页眉页脚和公式图片。
- 图注只读图片自带的 `image_caption`，不把附近段落或图片脚注猜成图注。
- 无图注、无章节时填空字符串，图片仍保留。同一路径在不同位置出现时按出现次数保留。
- 章节按解析结果阅读顺序更新，只接受标题条目，不把普通正文当标题。
- 明确的章节编号优先确定祖先关系，以应对 MinerU 把多级标题全标为一级的情况；
  祖先名称必须来自已读标题，不凭空补写。没有编号时使用解析给出的标题级别。
- 标题漏识别、目录误识别、阅读顺序错误等源解析缺陷仍会影响章节归属；本工具不声称能自动修复所有源错误。
- 缺失图片或输入格式不支持时明确报错，不悄悄丢弃图片；零图片的合法输入输出 `[]`。
- 保留原解析结果目录。单独移动 JSON 后，相对图片路径可能失效。

## 验证

```bash
python -m unittest discover -s tests -v
```

测试仅使用临时样例，PDF 入口使用模拟子进程验证，不调用模型或解析真实 PDF。

## HTTP 接口

主目录的 `image_export_api.py` 是独立接口，不导入或修改原 `extract_api.py`。
依赖 Flask、psutil 和已安装的 MinerU；当前服务器虚拟环境已具备这些依赖。

启动：

```bash
cd /root/autodl-tmp
source MinerU/.venv/bin/activate
export IMAGE_API_PORT=5003
export IMAGE_API_WORKERS=1
python image_export_api.py
```

在另一个终端上传：

```bash
curl --fail-with-body -X POST http://127.0.0.1:5003/api/extract-pdf-images-zip \
  -F "files=@/root/autodl-tmp/pdf_Elan/EM78P134N_10.pdf" \
  -o EM78P134N_10_images.zip
```

每次请求一个 PDF，兼容 `files` 或 `file` 字段。
可选表单字段：`backend`、`method`、`lang`，默认 `hybrid-auto-engine`、`auto`、`ch`。
`GET /health` 可检查服务状态，不运行模型。

ZIP 只包含：

```text
EM78P134N_10_images.json
images/
  crop1.jpg
  crop2.png
```

JSON 仍为每条三个字段，但图片路径已改为 `images/crop1.jpg`，相对于解压后的 JSON。
保留原图片格式；同一文件不重复打包，不同来源同名文件会安全改名。
不含原 PDF、解析中间 JSON 或日志。无图片时仍返回 `[]` 和空 `images/`。
ZIP 生成失败不会返回不完整结果；传输结束或断开连接后清理请求临时文件。
子进程失败日志末尾只写到服务日志，HTTP 不回传服务器路径或内部日志。

服务配置均在启动前通过环境变量设置：

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `IMAGE_API_HOST` | `0.0.0.0` | 监听地址 |
| `IMAGE_API_PORT` | `5003` | 监听端口 |
| `IMAGE_API_WORKERS` | `1` | 本接口允许的并行解析数 |
| `IMAGE_API_TIMEOUT` | `3600` | 单次解析超时秒数 |
| `IMAGE_API_QUEUE_TIMEOUT` | `300` | 排队超时秒数 |
| `IMAGE_API_MAX_MB` | `200` | 请求体大小上限，包含 multipart 开销 |
| `IMAGE_API_PYTHON` | 当前 Python | 安装 MinerU 的解释器路径 |
| `IMAGE_API_LOCK_DIR` | 工具下 `api_locks/` | 本接口进程共用的并发锁目录 |

错误状态：400 上传或参数不合法，413 请求过大，503 排队超时，504 解析超时，500 解析或打包失败。
并发锁仅管理本接口，不与原引脚接口协调 GPU 使用；同一 GPU 不宜同时启动两套解析任务。
默认服务无鉴权，外网使用应在网关增加鉴权、上传限制和足够的超时。
