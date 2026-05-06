# 每日 AI 资讯生成智能体

面向中文简报场景的 AI 资讯日报生成项目。系统会从配置的数据源抓取国内外 AI 资讯，完成初筛、去重、分类、摘要、质量评分，并输出 Word、Markdown 和统计 TXT 文件。

当前大模型调用已改为 OpenAI-compatible `chat/completions` 接口，不再直接依赖 DashScope SDK。你只需要在 `.env` 中修改大模型 API 配置，即可切换 Kimi、OpenAI、DashScope 兼容模式或其他 OpenAI 兼容服务。

## 免责声明

1. 本项目仅用于个人学习和内部测试，请勿用于商业用途或非法活动。
2. 使用前请遵守目标网站的 robots.txt、服务条款和相关法律法规。
3. 使用者需自行承担因违反网站规则、服务条款或法律法规产生的责任。
4. 项目作者不对使用本项目造成的任何后果承担责任。

## 当前能力

- 支持 OpenAI-compatible 大模型 API，用于中文摘要、英文内容翻译、分类和质量评分。
- 未配置可用大模型时自动降级为规则摘要模式，仍可生成日报。
- 支持 Kimi/Moonshot 这类有固定参数限制的模型，自动兼容 `temperature=1`、`top_p=0.95` 等服务端要求。
- 支持大模型请求限速、限并发、超时重试、429 重试和 5xx 重试。
- 内置国内资讯源、国外资讯源、投融资资讯源和论文资讯源。
- 输出 5 个栏目：AI 应用、AI 模型、AI 安全、AI 投融资、最新研究论文。
- 同步生成 Word、Markdown、抓取统计 TXT 和运行日志。
- Web 控制台支持保存配置、管理数据源、查看任务进度、下载生成结果。

## 目录结构

```text
AiNewsFind/
├─ ai_news_agent/              # 抓取、过滤、摘要、生成文档的核心代码
├─ config/
│  ├─ default_config.yaml       # 默认配置
│  └─ saved_web_config.yaml     # Web 控制台保存的配置
├─ logs/                       # 运行日志
├─ output/                     # 生成文件输出目录
├─ web_ui/                     # FastAPI Web 控制台
├─ run_web_ui.py               # Web 控制台启动入口
├─ run_scheduler.py            # 定时任务入口
├─ run_daily_news.py           # 命令行生成入口
├─ .env.example                # 大模型环境变量示例
└─ requirements.txt
```

## 安装依赖

```powershell
pip install -r requirements.txt
```

## 配置大模型 API

项目默认读取 `.env` 中的通用大模型配置，调用格式为 OpenAI-compatible `chat/completions`。

最少需要配置：

```dotenv
LLM_PROVIDER=openai_compatible
LLM_API_KEY=replace-with-your-api-key
LLM_BASE_URL=https://your-provider.example/v1
LLM_MODEL=replace-with-your-model-name
```

如果服务商直接提供完整接口地址，也可以使用：

```dotenv
LLM_API_URL=https://your-provider.example/v1/chat/completions
```

### Kimi / Moonshot 示例

```dotenv
LLM_PROVIDER=kimi
LLM_API_KEY=你的Kimi APIKey
LLM_BASE_URL=https://api.moonshot.cn/v1
LLM_MODEL=kimi-k2.5

LLM_TEMPERATURE=1
LLM_TOP_P=0.95
LLM_REQUESTS_PER_MINUTE=12
LLM_MAX_WORKERS=2
LLM_MAX_CONCURRENCY=2
LLM_MAX_RETRIES=6
LLM_TIMEOUT_SECONDS=120
```

### OpenAI 示例

```dotenv
LLM_PROVIDER=openai
LLM_API_KEY=你的OpenAI APIKey
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=你的模型名
```

### DashScope 兼容模式示例

当前项目不再使用 DashScope SDK，但仍可通过 DashScope 的 OpenAI 兼容模式调用 Qwen：

```dotenv
LLM_PROVIDER=dashscope
LLM_API_KEY=你的DashScope APIKey
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-turbo-latest
```

## 大模型参数说明

这些参数既可以写在 `.env`，也可以在 `config/default_config.yaml` 或 Web 保存配置中设置；`.env` 优先级更高。

```dotenv
LLM_REQUESTS_PER_MINUTE=12
LLM_MAX_WORKERS=2
LLM_MAX_CONCURRENCY=2
LLM_MAX_RETRIES=6
LLM_TIMEOUT_SECONDS=120
LLM_MAX_TOKENS=2048
LLM_TEMPERATURE=1
LLM_TOP_P=0.95
```

- `LLM_REQUESTS_PER_MINUTE`：每分钟最多发起多少次大模型请求。
- `LLM_MAX_WORKERS`：大模型分析线程数。
- `LLM_MAX_CONCURRENCY`：实际并发请求上限，用于避免服务商组织级并发限制。
- `LLM_MAX_RETRIES`：超时、429、5xx 等错误的最大重试次数。
- `LLM_TIMEOUT_SECONDS`：单次大模型请求超时时间。
- `LLM_TEMPERATURE`、`LLM_TOP_P`：采样参数。部分模型只接受固定值，代码会按服务端错误提示自动重试修正。

Kimi/Moonshot 对组织级并发较敏感，建议保持 `LLM_MAX_CONCURRENCY=2`。如果只是快速测试，可以把 `runtime.max_articles_for_analysis` 降低到 10 左右。

## Web 控制台

启动本地控制台：

```powershell
python run_web_ui.py
```

打开：

```text
http://127.0.0.1:8001
```

控制台支持：

- 配置开始日期、结束日期和回退抓取时长。
- 配置每站抓取上限、候选分析上限、每个栏目最多入选数量。
- 配置摘要字数范围、质量评分阈值，以及是否启用大模型摘要。
- 新增、删除、启用、停用、排序数据源。
- 保存配置并生成日报。
- 查看进度、下载 Word、Markdown、统计 TXT 和日志文件。

Web 保存后的配置写入：

```text
config/saved_web_config.yaml
```

生成文件写入：

```text
output/
```

日志写入：

```text
logs/ai_news_agent.log
```

## 命令行生成

使用默认配置生成日报：

```powershell
python run_daily_news.py
```

使用 Web 保存配置生成日报：

```powershell
python run_daily_news.py --config config/saved_web_config.yaml
```

跳过大模型，使用规则摘要：

```powershell
python run_daily_news.py --skip-llm
```

限制每个栏目最多入选数量：

```powershell
python run_daily_news.py --max-items-per-section 3
```

## 定时运行

定时任务使用 `config/default_config.yaml` 中的 `schedule.daily_time`，默认是 `09:00`。

```powershell
python run_scheduler.py
```

## 配置说明

默认配置文件：

```text
config/default_config.yaml
```

Web 保存配置：

```text
config/saved_web_config.yaml
```

主要配置块：

- `runtime`：输出目录、日志目录、抓取超时、抓取并发、日期范围、候选分析上限、每栏入选上限。
- `summary`：摘要最少和最多字数。
- `quality`：资讯质量评分最低阈值。
- `llm`：大模型 provider、模型名环境变量、API Key 环境变量、Base URL 环境变量、超时、重试、限速和并发参数。
- `document`：文档标题、字体、字号、行距、栏目顺序。
- `filtering`：包含关键词、排除关键词、分类关键词。
- `sources`：数据源地址、抓取类型、权重、选择器、URL 规则和过滤规则。

## 输出结果

成功生成后，`output/` 下会出现类似文件：

```text
每日AI资讯_20260429_1705.docx
每日AI资讯_20260429_1705.md
每日AI资讯_20260429_1705_stats.txt
```

Word 和 Markdown 中会标注本次摘要模式，例如：

```text
Kimi API / kimi-k2.5
OpenAI API / gpt-4.1
规则摘要
```

## 性能与排查

- 抓取慢通常来自外部站点超时，例如 HuggingFace、arXiv、Hacker News 等站点访问不稳定。
- 大模型慢通常来自限速、并发限制、模型响应慢或重试。
- 生产稳定优先时，建议保持低并发：`LLM_MAX_CONCURRENCY=2`。
- 快速测试时，建议降低 `runtime.max_articles_for_analysis`，例如改为 `10`。
- 日志是追加写入的，旧错误不会自动删除；排查时请看本次运行开始时间之后的日志。

## 已知说明

- 对需要登录、强反爬或强 JS 渲染的网站，当前版本可能只能抓取到部分内容。
- 外部站点偶发超时或断连时，系统会记录日志并继续处理其他数据源。
- 大模型不可用时会自动回退规则摘要，确保日报流程不中断。
