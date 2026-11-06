项目架构：

```
local/
  extract.py                 # 单文件入口，已支持 --vendor
  batch_extract.py           # 批量入口，已支持 --vendor
  extract_api.py             # API 入口，已支持 vendor 参数
  parse_doc.py               # MinerU 解析入口
  mineru_api_zip.py
  batch_parse.py

  extract/
    __init__.py
    pin_package_extractor.py
    package_catalog_resolver.py
    multi_package_extractor.py
    multi_PkgTab_extractor.py
    semantic_classifier.py
    table_header_structure.py
    group_title_context.py
    special_table_handlers.py
    parallel_cell_splitter.py
    repeated_horizontal_table_filter.py
    note_extractor.py

    vendors/
      __init__.py
      base.py
      registry.py

      ti/
        __init__.py
        profile.py

      st/
        __init__.py
        profile.py

    test/
      test_*.py

  post_table/
    fix_ocr_table.py
    min_max_coordinate_correct.py
    restore_cell_line_breaks.py
    split_merged_tables.py
    ...

  modify/
    Filter_ headers_and_footers.py

  MinerU/
    mineru/
    docs/
    tests/
    ...
```







+ qoder ssh连接

```
root@connect.westd.seetacloud.com:40499
root@connect.westc.seetacloud.com:53424
root@connect.westb.seetacloud.com:42911
root@connect.westc.seetacloud.com:49251
password
ssh -p 49251 root@connect.westc.seetacloud.com
0/TOYs+JtBD4

```



+ 开机流程

```
source /root/miniconda3/etc/profile.d/conda.sh
conda activate /root/autodl-tmp/envs/mineru

export MINERU_MODEL_SOURCE=modelscope
export HF_HOME=/root/autodl-tmp/.cache/huggingface
export HUGGINGFACE_HUB_CACHE=/root/autodl-tmp/.cache/huggingface/hub
export MODELSCOPE_CACHE=/root/autodl-tmp/.cache/modelscope
export TORCH_HOME=/root/autodl-tmp/.cache/torch

cd /root/autodl-tmp/
```



+ pipeline模式

```
cd /root/autodl-tmp
python parse_doc.py pdfs/5b1d01ebfc56798c0741e577911781f2.pdf -o outputs
```

+ hybrid-auto-engine模式

```
cd /root/autodl-tmp
python parse_doc.py pdfs/BGA 4304A03U  (含内圈).pdf -o outputs -b hybrid-auto-engine

cd /root/autodl-tmp
python parse_doc.py pdfs/5b1d01ebfc56798c0741e577911781f2.pdf -o outputs -b hybrid-auto-engine

cd /root/autodl-tmp
python parse_doc.py pdfs/tms320dm8148.pdf -o outputs -b hybrid-auto-engine

cd /root/autodl-tmp
source MinerU/.venv/bin/activate
python batch_parse.py
```



+ 加入LLM判断——抽取

```
cd /root/autodl-tmp
source .env
source MinerU/.venv/bin/activate
echo $DEEPSEEK_MODELS

# ST
python batch_extract.py \
  -i pdf_ST \
  -o outputs_ST \
  --vendor ST \
  --semantic-classify \
  --skip-parse

# ABOV
python batch_extract.py \
  -i pdf_ABOV \
  -o outputs_ABOV \
  --vendor ABOV \
  --semantic-classify

python batch_extract.py \
  -i pdf_ABOV \
  -o outputs_ABOV \
  --vendor ABOV \
  --skip-parse \
  --semantic-classify
-----------------------------------------------------------
# Elan
python batch_extract.py \
  -i pdf_Elan \
  -o outputs_Elan \
  --vendor ELAN \
  --semantic-classify

---------------------------------------------------------
# Giga
python batch_extract.py \
  -i pdf_Giga \
  -o outputs_Giga \
  --vendor GIGA \
  --semantic-classify

----------------------------------------
# Infineon
python batch_extract.py \
  -i pdf_Infineon \
  -o outputs_Infineon \
  --vendor INFINEON \
  --semantic-classify

-------------------------------------
# Nexperia
python batch_extract.py \
  -i pdf_Nexperia \
  -o outputs_Nexperia \
  --vendor NEXPERIA \
  --semantic-classify




```

+ 抽取api

```
端口 5002

cd /root/autodl-tmp
source .env
export EXTRACT_SEMANTIC_CLASSIFY=true
export EXTRACT_SCHEMA_WORKERS=4
source MinerU/.venv/bin/activate
python extract_api.py

curl -X POST http://localhost:5002/api/extract-pdf-json \
  -F "file=@/root/autodl-tmp/Multi_package_TIpdf/BGA 4304A03U  (含内圈).pdf " \
  -o bga.json
```



+ table

```
cd /root/autodl-tmp
source MinerU/.venv/bin/activate
python table_export_api.py

curl -X POST http://localhost:5003/api/extract-pdf-tables \
  -F "file=@/root/autodl-tmp/pdfs/MLF 3809K00C （表格数据）.pdf" \
  -o example_tables.json
```



+++

# 多并发

```
cd /root/autodl-tmp
source .env
source MinerU/.venv/bin/activate
echo $DEEPSEEK_MODELS

export EXTRACT_VENDOR=TI
export EXTRACT_SEMANTIC_CLASSIFY=true
export MINERU_VLLM_GPU_MEMORY_UTILIZATION=0.4
export EXTRACT_API_WORKERS=2
export EXTRACT_API_LLM_WORKERS=2

python extract_api.py
```

```
cd /root/autodl-tmp/Multi_package_TIpdf

curl -X POST http://127.0.0.1:5002/api/extract-pdf-json-batch \
  -F "vendor=TI" \
  -F "files=@DRV2605L_9_10.pdf" \
  -o DRV2605L_9_10.json &

curl -X POST http://127.0.0.1:5002/api/extract-pdf-json-batch \
  -F "vendor=TI" \
  -F "files=@DRV2604L_9_10.pdf" \
  -o DRV2604L_9_10.json &

curl -X POST http://127.0.0.1:5002/api/extract-pdf-json-batch \
  -F "vendor=TI" \
  -F "files=@DRV8143-Q1_14_28_14.pdf" \
  -o DRV8143-Q1_14_28_14.json &

wait
```





```
cd /root/autodl-tmp
source .env
source MinerU/.venv/bin/activate
echo $DEEPSEEK_MODELS

export EXTRACT_SEMANTIC_CLASSIFY=true
export MINERU_VLLM_GPU_MEMORY_UTILIZATION=0.4
export EXTRACT_API_WORKERS=2
export EXTRACT_API_LLM_WORKERS=2

python extract_api.py
```

```
cd /root/autodl-tmp/Multi_package_TIpdf

curl -X POST http://127.0.0.1:5002/api/extract-pdf-json-batch \
  -F "files=@DRV2605L_9_10.pdf" \
  -o DRV2605L_9_10.json &

curl -X POST http://127.0.0.1:5002/api/extract-pdf-json-batch \
  -F "files=@DRV2604L_9_10.pdf" \
  -o DRV2604L_9_10.json &

curl -X POST http://127.0.0.1:5002/api/extract-pdf-json-batch \
  -F "files=@DRV8143-Q1_14_28_14.pdf" \
  -o DRV8143-Q1_14_28_14.json &

wait
```

````
现在的多并发流程是这样的：

服务端启动时先设定两个并发参数：

```bash
export EXTRACT_API_WORKERS=2
export EXTRACT_API_LLM_WORKERS=2
```

然后启动：

```bash
python extract_api.py
```

当前接口规则是：每个 HTTP 请求只能传 1 个 PDF。多并发不是一次请求传多个 PDF，而是客户端同时发多个请求。

比如你同时发 3 个请求：

```text
请求1：DRV2605L_9_10.pdf
请求2：DRV2604L_9_10.pdf
请求3：DRV8143-Q1_14_28_14.pdf
```

整体流程是：

```text
客户端同时发 3 个请求
        ↓
extract_api.py 收到 3 个请求
        ↓
每个请求先分配一个固定 llm_key_index
        ↓
进入 api_request_slot 全流程锁
        ↓
最多 2 个 PDF 同时进入 MinerU + extract.py
        ↓
第三个 PDF 等待有一个 api slot 释放
        ↓
每个 PDF 内部调用模型时，再进入 LLM 请求锁
        ↓
最多 2 个 LLM 请求同时发出
        ↓
每个 PDF 完成后直接返回格式化后的最终 JSON
```

更具体一点。

假如 `EXTRACT_API_WORKERS=2`，同时发 3 个 PDF：

```text
时间 T0：
pdf1 收到请求，进入执行
pdf2 收到请求，进入执行
pdf3 收到请求，但 api slot 满了，等待

时间 T1：
pdf1 和 pdf2 各自跑 MinerU 解析

时间 T2：
谁先完成 MinerU，谁先进入 extract 阶段

时间 T3：
extract 阶段遇到第一次模型调用，进入 LLM 锁

时间 T4：
如果两个 LLM slot 都空闲，两个 PDF 可以同时调用模型
如果 LLM slot 满了，后来的模型调用等待

时间 T5：
pdf1 或 pdf2 完成后释放 api slot

时间 T6：
pdf3 进入执行
```

现在有两类锁：

第一类是完整流程锁：

```text
EXTRACT_API_WORKERS=2
```

控制的是：

```text
最多几个 PDF 同时跑完整 parse + extract 流程
```

也就是控制 MinerU 并发。这个最吃显存。

第二类是模型调用锁：

```text
EXTRACT_API_LLM_WORKERS=2
```

控制的是：

```text
最多几个 LLM HTTP 请求同时发出去
```

这个主要控制 API 并发，不怎么吃本机显存。

关于模型 key 的分配，现在逻辑是“PDF 级固定分配”。

比如 `.env` 里是：

```bash
export DEEPSEEK_API_KEYS="key1,key2"
export DEEPSEEK_BASE_URLS="url1,url2"
export DEEPSEEK_MODELS="model1,model2"
```

并且：

```bash
export EXTRACT_API_LLM_WORKERS=2
```

那么连续来的请求会这样分配：

```text
第 1 个 PDF -> llm_key_index=0 -> key1 / url1 / model1
第 2 个 PDF -> llm_key_index=1 -> key2 / url2 / model2
第 3 个 PDF -> llm_key_index=0 -> key1 / url1 / model1
第 4 个 PDF -> llm_key_index=1 -> key2 / url2 / model2
```

同一个 PDF 里面有两次模型调用：

1. 表字段/表类型语义分类；
2. 封装目录判断。

这两次都会使用同一个 `llm_key_index`，不会中途切换模型。

也就是说：

```text
pdf1 第一次模型调用用 key1
pdf1 第二次模型调用也用 key1

pdf2 第一次模型调用用 key2
pdf2 第二次模型调用也用 key2
```

但有一个细节：现在 key 分配是轮转，不是“谁空闲就用谁”。

也就是说第三个 PDF 不会动态判断 key1/key2 哪个先空闲。它收到请求时就已经按计数器分好了：

```text
pdf3 -> key1
```

然后它后面一直用 key1。

当前逻辑下：

```text
api slot 控制 PDF 并发
llm slot 控制 LLM 请求总并发
llm_key_index 控制每个 PDF 固定用哪个 API key/model
```

你现在应该这样理解：

```text
多个请求同时进来
    ↓
每个请求只处理一个 PDF
    ↓
服务端最多同时跑 EXTRACT_API_WORKERS 个 PDF
    ↓
每个 PDF 固定绑定一个模型 API
    ↓
模型请求最多同时发 EXTRACT_API_LLM_WORKERS 个
    ↓
每个请求完成后直接返回一个格式化 JSON
```

推荐启动命令还是：

```bash
cd /root/autodl-tmp
source .env
source MinerU/.venv/bin/activate
echo $DEEPSEEK_MODELS

export EXTRACT_SEMANTIC_CLASSIFY=true
export MINERU_VLLM_GPU_MEMORY_UTILIZATION=0.4
export EXTRACT_API_WORKERS=2
export EXTRACT_API_LLM_WORKERS=2

python extract_api.py
```

推荐并发请求方式：

```bash
cd /root/autodl-tmp/Multi_package_TIpdf

curl -X POST http://127.0.0.1:5002/api/extract-pdf-json-batch \
  -F "files=@DRV2605L_9_10.pdf" \
  -o DRV2605L_9_10.json &

curl -X POST http://127.0.0.1:5002/api/extract-pdf-json-batch \
  -F "files=@DRV2604L_9_10.pdf" \
  -o DRV2604L_9_10.json &

curl -X POST http://127.0.0.1:5002/api/extract-pdf-json-batch \
  -F "files=@DRV8143-Q1_14_28_14.pdf" \
  -o DRV8143-Q1_14_28_14.json &

wait
```

如果显存不稳，就把：

```bash
export EXTRACT_API_WORKERS=2
```

改成：

```bash
export EXTRACT_API_WORKERS=1
```

这样 MinerU 只会一个 PDF 一个 PDF 跑，但 LLM 仍然可以保留：

```bash
export EXTRACT_API_LLM_WORKERS=2
```
````



