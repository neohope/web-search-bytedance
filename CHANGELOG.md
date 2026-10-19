# Changelog

## 1.4.0 — 2026-10-19

首个公开版本，基于火山引擎官方 skill `v1.3.4` 打包。

### 兼容性

- 技能名、仓库名与安装目录统一为 `web-search-bytedance`；
- 脚本去掉 `requests` 依赖，改用标准库 `urllib`，Python 3.9+ 即可运行；
- Windows 下强制 UTF-8 输出，修复中文结果乱码；
- 文档按宿主拆分：DSH / OpenClaw / Hermes / 通用。

### 行为

- 新增 `--json`、`--max-chars`、`--timeout`、`--retries`；
- 408/429/5xx 与网络错误自动重试一次（可用 `--retries` 调整）；
- 单条摘要超过 1000 字符、整体输出超过 `--max-chars` 时显式标记截断；
- 文本输出补充发布日期（`PublishTime`），无链接的结果标注「无法引用」；
- 移除 `--prompt-api-key` 交互式入口：agent 宿主的 stdin 可能仍报告为控制台，进程会一直等待输入；Key 统一走环境变量或 `--api-key`；
- 退出码规范：`0` 成功、`1` 凭证或接口失败、`2` 参数错误。

### 安全

- 凭证文件只读取 `WEB_SEARCH_API_KEY`、`VOLCENGINE_ACCESS_KEY`、`VOLCENGINE_SECRET_KEY`；
- 文档不再把「把 Key 发到聊天框」作为推荐路径；
- 披露每个请求携带的 `X-Traffic-Tag: skill_web_search_common`。

### 仓库

- 新增 `.env.example`、`.gitignore`、`.gitattributes`、`agents/openai.yaml`；
- 新增 `tests/`（本地 mock 接口）与 `.github/workflows/ci.yml`。

## 上游基线

火山引擎官方 skill `v1.3.4`（`author: volcengine-search-team`，`homepage: https://www.volcengine.com/docs/85508/1650263`）。
