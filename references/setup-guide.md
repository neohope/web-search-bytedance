# web-search-bytedance — 开通与配置

## 1. 注册与开通

1. 打开 <https://www.volcengine.com> 注册（手机 / 飞书 / 抖音），完成实名认证。
2. [联网搜索控制台](https://console.volcengine.com/search-infinity/web-search) →【正式开通】。个人用户每月自动获得 500 次免费额度。

## 2. 获取凭证

**方式 A（推荐）**：[API Key 管理](https://console.volcengine.com/search-infinity/api-key) →【创建 API Key】→ 复制保存。

**方式 B（AK/SK）**：控制台头像 → API 访问密钥 → 创建。需要配置 `VOLCENGINE_ACCESS_KEY` 和 `VOLCENGINE_SECRET_KEY`；SK 只显示一次，请及时保存。子账号需授权 `TorchlightApiFullAccess`。

**方式 C（Coding Plan）**：[Coding Plan 控制台](https://console.volcengine.com/ark/region:ark+cn-beijing/openManagement?LLM=%7B%7D&advancedActiveKey=subscribe&projectName=default)（企业版用 [Coding Plan 企业版](https://console.volcengine.com/ark/region:ark+cn-beijing/openManagement?LLM=%7B%7D&advancedActiveKey=enterprise&projectName=default)）→【专属权益】→【联网搜索】→【查看 API Key】→ 复制。

> 只有联网搜索控制台或 Coding Plan 签发的 Key 可用；火山方舟（Ark）等其他来源的 Key 不通用。

## 3. 按宿主配置

任何宿主都只需要把 Key 放进进程环境变量 `WEB_SEARCH_API_KEY`；下面是各宿主的常见做法。

### DSH

在项目根 `.env` 写 `WEB_SEARCH_API_KEY=...`（由 shell 或执行工具注入），或在技能目录复制 `.env.example` 为 `.env` 后填写（脚本会读取，且该文件已被 `.gitignore` 排除）。不要用聊天消息传递 Key——会话日志会持久化。

### OpenClaw

编辑 `~/.openclaw/openclaw.json`，在 `skills.entries` 下加入：

```json
"web-search-bytedance": {
  "enabled": true,
  "env": { "WEB_SEARCH_API_KEY": "你的Key" }
}
```

也可以写 `~/.openclaw/.env`（脚本会读取 `~/.openclaw/.env`，旧版本路径 `/root/.openclaw/.env` 同样支持）。

### Hermes

设置进程环境变量 `WEB_SEARCH_API_KEY`，或在技能目录创建 `.env`（可复制 `.env.example` 后填写）。若 Hermes 有自己的凭证/环境变量注入配置，优先用它，效果等价。

### 其他宿主（Claude Code、Codex 等）

设置进程环境变量，或在技能目录放 `.env`。任何能向子进程注入 `WEB_SEARCH_API_KEY` 的方式都可以。

## 4. 验证

```bash
python scripts/web_search.py "北京今日天气"
```

Windows 用 `python`，macOS / Linux 用 `python3`。看到「结果数: N」即为成功；返回「未找到凭证」说明环境变量没有进入执行脚本的进程。

## 5. 常见问题

| 问题 | 解答 |
|---|---|
| SK 忘了 | 无法找回，删除旧密钥重建 |
| 权限错误 / `10403` | 检查已开通、Key 来自联网搜索控制台、子账号已授权 `TorchlightApiFullAccess` |
| 额度用完 / `10406`、`10412` | 正式开通后按量计费，或在控制台购买套餐 |
| 欠费 / `10408` | 后付费 24h 内充值可恢复 |
| Windows 输出乱码 | 脚本已强制 UTF-8 输出；若仍乱码，确认调用的是本脚本而不是旧副本 |
| 429 限流 | 单 Key 并发控制在 5 以内；脚本会自动重试一次，仍失败则降频 |
| 结果为空 | 换关键词、放宽 `--time-range`、去掉修饰词，或调大 `--count` |
