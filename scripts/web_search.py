#!/usr/bin/env python3
# Copyright (c) 2025 Beijing Volcano Engine Technology Co., Ltd. and/or its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modified for standalone skill packaging: standard-library HTTP transport,
# UTF-8 console output, credential-file allowlist, retries, --json and
# --max-chars. The Volcengine request body and HMAC signing are unchanged.

"""火山引擎联网搜索 API 客户端，适用于 DSH、OpenClaw、Hermes 等任意 `SKILL.md` 宿主。

官方文档：https://www.volcengine.com/docs/85508/1650263
签名参考：https://github.com/volcengine/volc-openapi-demos/blob/main/signature/python/sign.py

只依赖 Python 标准库。凭证解析顺序：

1. `--api-key`，否则环境变量 `WEB_SEARCH_API_KEY`
2. `VOLCENGINE_ACCESS_KEY` + `VOLCENGINE_SECRET_KEY`（AK/SK 签名）
3. VeFaaS IAM：仅当环境里存在可选依赖 `veadk` 时

额外的凭证文件按顺序读取 `<skill>/.env`、`~/.openclaw/.env`、`/root/.openclaw/.env`，
只取 `WEB_SEARCH_API_KEY`、`VOLCENGINE_ACCESS_KEY`、`VOLCENGINE_SECRET_KEY` 三个变量，
不会把文件里的其他变量注入进程环境。

示例：

    python scripts/web_search.py "北京天气"
    python scripts/web_search.py "OpenAI 最新发布" --time-range OneWeek --json
    python scripts/web_search.py "故宫博物院" --type image --count 3
"""

import argparse
import datetime
import hashlib
import hmac
import json
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import quote

SERVICE = "volc_torchlight_api"
VERSION = "2025-01-01"
REGION = "cn-beijing"
HOST = "mercury.volcengineapi.com"
ACTION = "WebSearch"
INTERNAL_API_URL = "https://open.feedcoopapi.com/search_api/web_search"
TRAFFIC_TAG_HEADER = "X-Traffic-Tag"
TRAFFIC_TAG_VALUE = "skill_web_search_common"
TIME_RANGE_SHORTCUTS = {"OneDay", "OneWeek", "OneMonth", "OneYear"}
DATE_RANGE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")
CREDENTIAL_ENV_NAMES = ("WEB_SEARCH_API_KEY", "VOLCENGINE_ACCESS_KEY", "VOLCENGINE_SECRET_KEY")
SUMMARY_PREVIEW_LIMIT = 1000
TRUNCATION_MARK = " …[已截断]"
DEFAULT_MAX_CHARS = 20000
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 1
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
ERROR_HINTS = {
    "10400": "提示：参数错误。请检查 Query、Count、TimeRange 等参数格式是否正确。",
    "10402": "提示：搜索类型非法。当前仅支持 web 或 image。",
    "10403": "提示：账号或权限异常。请确认 API Key 来自联网搜索控制台，或检查账号权限。",
    "10406": "提示：免费额度已耗尽。请检查账户额度或联系支持。",
    "10407": "提示：当前无可用免费策略。请检查账户状态或联系支持。",
    "10500": "提示：服务内部错误。建议稍后重试，或联系支持。",
    "700429": "提示：免费链路触发限流。请降频后重试。",
    "100013": "提示：子账号未授权 TorchlightApiFullAccess。",
}


class SearchError(Exception):
    """A failed search request with its HTTP status and decoded response body."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


# ---- 凭证文件 ----

def credential_file_paths() -> list:
    """Return the dotenv candidates in priority order: skill root first."""
    paths = [str(Path(__file__).resolve().parents[1] / ".env")]
    try:
        paths.append(str(Path.home() / ".openclaw" / ".env"))
    except RuntimeError:
        pass
    paths.append("/root/.openclaw/.env")
    return paths


def load_env_file(env_path: str) -> None:
    """Load the three credential variables from one dotenv file, if it exists."""
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if key not in CREDENTIAL_ENV_NAMES:
            continue
        value = value.strip()
        try:
            parsed = shlex.split(value, comments=True)
            value = parsed[0] if parsed else ""
        except ValueError:
            value = value.strip("\"'")
        os.environ.setdefault(key, value)


def load_credential_files() -> None:
    """Read every dotenv candidate once; the process environment still wins."""
    seen = set()
    for env_path in credential_file_paths():
        normalized = os.path.abspath(os.path.expanduser(env_path))
        if normalized in seen:
            continue
        seen.add(normalized)
        load_env_file(normalized)


# ---- 火山引擎 HMAC-SHA256 签名（基于官方示例） ----

def _hmac_sha256(key: bytes, content: str) -> bytes:
    return hmac.new(key, content.encode("utf-8"), hashlib.sha256).digest()


def _hash_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _norm_query(params: dict) -> str:
    query = ""
    for key in sorted(params.keys()):
        if isinstance(params[key], list):
            for value in params[key]:
                query += quote(key, safe="-_.~") + "=" + quote(value, safe="-_.~") + "&"
        else:
            query += quote(key, safe="-_.~") + "=" + quote(str(params[key]), safe="-_.~") + "&"
    return query[:-1].replace("+", "%20") if query else ""


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _sign_request(method: str, ak: str, sk: str, body: str, session_token: str = "") -> dict:
    now = _utc_now()
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    x_content_sha256 = _hash_sha256(body)
    content_type = "application/json"

    query_params = {"Action": ACTION, "Version": VERSION}

    signed_header_keys = ["content-type", "host", "x-content-sha256", "x-date", "x-traffic-tag"]
    if session_token:
        signed_header_keys.append("x-security-token")
    signed_header_keys.sort()
    signed_headers_str = ";".join(signed_header_keys)

    canonical_header_lines = [
        f"content-type:{content_type}",
        f"host:{HOST}",
        f"x-content-sha256:{x_content_sha256}",
        f"x-date:{x_date}",
        f"x-traffic-tag:{TRAFFIC_TAG_VALUE}",
    ]
    if session_token:
        canonical_header_lines.append(f"x-security-token:{session_token}")
        canonical_header_lines.sort()

    canonical_request = "\n".join(
        [
            method.upper(),
            "/",
            _norm_query(query_params),
            "\n".join(canonical_header_lines),
            "",
            signed_headers_str,
            x_content_sha256,
        ]
    )

    credential_scope = f"{short_date}/{REGION}/{SERVICE}/request"
    string_to_sign = "\n".join(
        [
            "HMAC-SHA256",
            x_date,
            credential_scope,
            _hash_sha256(canonical_request),
        ]
    )

    k_date = _hmac_sha256(sk.encode("utf-8"), short_date)
    k_region = _hmac_sha256(k_date, REGION)
    k_service = _hmac_sha256(k_region, SERVICE)
    k_signing = _hmac_sha256(k_service, "request")
    signature = _hmac_sha256(k_signing, string_to_sign).hex()

    authorization = (
        f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
        f"SignedHeaders={signed_headers_str}, "
        f"Signature={signature}"
    )

    headers = {
        "Content-Type": content_type,
        "Host": HOST,
        "X-Date": x_date,
        "X-Content-Sha256": x_content_sha256,
        TRAFFIC_TAG_HEADER: TRAFFIC_TAG_VALUE,
        "Authorization": authorization,
    }
    if session_token:
        headers["X-Security-Token"] = session_token
    return headers


# ---- 凭证解析 ----

def get_credentials() -> tuple:
    """Return `(ak, sk, session_token)` from the environment or VeFaaS IAM; `(None, None, "")` when absent."""
    ak = os.getenv("VOLCENGINE_ACCESS_KEY")
    sk = os.getenv("VOLCENGINE_SECRET_KEY")
    if ak and sk:
        return ak, sk, ""

    try:
        from veadk.auth.veauth.utils import get_credential_from_vefaas_iam

        cred = get_credential_from_vefaas_iam()
        return cred.access_key_id, cred.secret_access_key, cred.session_token
    except Exception:
        # veadk is an optional dependency of the VeFaaS deployment; without it
        # the caller falls back to the explicit credential message.
        return None, None, ""


def resolve_api_key(cli_api_key: Optional[str]) -> Optional[str]:
    """Resolve the API key from the flag or the environment; agent hosts have no prompt."""
    api_key = cli_api_key or os.getenv("WEB_SEARCH_API_KEY")
    if not api_key:
        return None
    return api_key.strip() or None


def _validate_time_range(time_range: Optional[str]) -> Optional[str]:
    if not time_range:
        return None
    if time_range in TIME_RANGE_SHORTCUTS:
        return time_range

    match = DATE_RANGE_PATTERN.match(time_range)
    if not match:
        raise ValueError(
            "--time-range 需为 OneDay/OneWeek/OneMonth/OneYear，或日期区间 YYYY-MM-DD..YYYY-MM-DD。"
        )

    start_text, end_text = match.groups()
    try:
        start_date = datetime.date.fromisoformat(start_text)
        end_date = datetime.date.fromisoformat(end_text)
    except ValueError as exc:
        raise ValueError("--time-range 中的日期需为有效的 YYYY-MM-DD。") from exc

    if start_date > end_date:
        raise ValueError("--time-range 的开始日期不能晚于结束日期。")

    return time_range


def build_body(
        query: str,
        search_type: str = "web",
        count: int = 10,
        time_range: Optional[str] = None,
        auth_level: int = 0,
        query_rewrite: bool = False,
) -> dict:
    body = {"Query": query, "SearchType": search_type, "Count": count}

    if search_type == "web":
        body["NeedSummary"] = True
        filters = {}
        if auth_level > 0:
            filters["AuthInfoLevel"] = auth_level
        if filters:
            body["Filter"] = filters
        if time_range:
            body["TimeRange"] = time_range

    if query_rewrite:
        body["QueryControl"] = {"QueryRewrite": True}

    return body


# ---- API 调用 ----

def _read_error_body(error: urllib.error.HTTPError) -> str:
    try:
        return error.read().decode("utf-8", errors="replace")[:2000]
    except OSError:
        return ""


def _retry_delay(attempt: int) -> float:
    return min(1.5 * attempt, 5.0)


def _post_json(url: str, headers: dict, body_str: str, timeout: float, retries: int) -> dict:
    """POST one JSON request and decode the reply, retrying transient failures."""
    attempt = 0
    while True:
        attempt += 1
        request = urllib.request.Request(
            url,
            data=body_str.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = _read_error_body(error)
            if error.code in RETRYABLE_STATUS and attempt <= retries:
                time.sleep(_retry_delay(attempt))
                continue
            raise SearchError(f"HTTP {error.code}", error.code, body) from error
        except (urllib.error.URLError, OSError) as error:
            if attempt <= retries:
                time.sleep(_retry_delay(attempt))
                continue
            reason = getattr(error, "reason", error)
            raise SearchError(f"网络错误：{reason}") from error
        except ValueError as error:
            raise SearchError(f"响应不是合法 JSON：{error}") from error


def do_search(
        body: dict,
        api_key: Optional[str] = None,
        ak: Optional[str] = None,
        sk: Optional[str] = None,
        session_token: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
) -> dict:
    body_str = json.dumps(body, ensure_ascii=False)
    if api_key:
        headers = {
            "Content-Type": "application/json",
            TRAFFIC_TAG_HEADER: TRAFFIC_TAG_VALUE,
            "Authorization": f"Bearer {api_key}",
        }
        url = INTERNAL_API_URL
    else:
        if not ak or not sk:
            raise SearchError("缺少 AK/SK")
        headers = _sign_request("POST", ak, sk, body_str, session_token)
        url = f"https://{HOST}?Action={ACTION}&Version={VERSION}"

    return _post_json(url, headers, body_str, timeout, retries)


# ---- 输出格式化 ----

def _preview(text: str, limit: int = SUMMARY_PREVIEW_LIMIT) -> str:
    """Keep one summary short enough for a context budget, marking any cut."""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + TRUNCATION_MARK


def cap_output(text: str, limit: int) -> str:
    """Bound the whole text view, reporting how much was omitted."""
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = limit
    return f"{text[:cut]}\n…[输出已截断：剩余 {len(text) - cut} 字符未显示；调大 --max-chars 或减小 --count]"


def format_text(data: dict, search_type: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    result = data.get("Result") or {}
    lines = [f"结果数: {result.get('ResultCount', 0)}  耗时: {result.get('TimeCost', 0)}ms", ""]

    if search_type == "web":
        for item in result.get("WebResults") or []:
            lines.append(f"[{item.get('SortId', '')}] {item.get('Title', '')}")
            meta_parts = [part for part in [item.get("SiteName", ""), item.get("AuthInfoDes", "")] if part]
            published = str(item.get("PublishTime") or "")[:10]
            if published:
                meta_parts.append(published)
            if meta_parts:
                lines.append(f"    {' | '.join(meta_parts)}")
            if item.get("Url"):
                lines.append(f"    {item['Url']}")
            else:
                lines.append("    (该结果无链接，无法引用)")
            summary = item.get("Summary") or item.get("Snippet", "")
            if summary:
                lines.append(f"    {_preview(summary)}")
            lines.append("")
    else:
        for item in result.get("ImageResults") or []:
            image = item.get("Image") or {}
            lines.append(f"[{item.get('SortId', '')}] {item.get('Title', '')}")
            if image.get("Url"):
                lines.append(f"    {image['Url']}")
            lines.append(f"    {image.get('Width', '?')}x{image.get('Height', '?')} ({image.get('Shape', '')})")
            lines.append("")

    return cap_output("\n".join(lines), max_chars)


# ---- CLI ----

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="火山引擎联网搜索 API\nhttps://www.volcengine.com/docs/85508/1650263\n"
                    "凭证：WEB_SEARCH_API_KEY / --api-key，或 VOLCENGINE_ACCESS_KEY + VOLCENGINE_SECRET_KEY",
    )
    parser.add_argument("query", help="搜索关键词")
    parser.add_argument("--type", "-t", default="web", choices=["web", "image"])
    parser.add_argument("--count", "-c", type=int, default=10)
    parser.add_argument(
        "--time-range",
        help="OneDay/OneWeek/OneMonth/OneYear/YYYY-MM-DD..YYYY-MM-DD",
    )
    parser.add_argument("--auth-level", type=int, default=0, choices=[0, 1])
    parser.add_argument("--query-rewrite", action="store_true", help="开启 Query 改写")
    parser.add_argument("--api-key", help="API Key（优先于环境变量 WEB_SEARCH_API_KEY）")
    parser.add_argument("--json", action="store_true", help="输出接口原始 JSON，便于脚本处理")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help=f"文本输出总长度上限（默认 {DEFAULT_MAX_CHARS}，0 表示不限）",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"单请求超时秒数（默认 {DEFAULT_TIMEOUT:g}）")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help=f"瞬态失败重试次数（默认 {DEFAULT_RETRIES}）")
    return parser


def usage_error(message: str) -> int:
    print(f"Error: {message}", file=sys.stderr)
    return 2


def main(argv: Optional[list] = None) -> int:
    load_credential_files()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.query or not args.query.strip():
        return usage_error("请输入搜索词。")
    if len(args.query) > 100:
        return usage_error("搜索词超过 100 字符，API 可能截断。建议精简后重试。")
    if args.count < 1:
        return usage_error("--count 需 ≥ 1。")
    if args.type == "image" and args.count > 5:
        return usage_error("image 类型最多返回 5 条，请调整 --count。")
    if args.type == "web" and args.count > 50:
        return usage_error("web 类型最多返回 50 条，请调整 --count。")
    if args.max_chars < 0:
        return usage_error("--max-chars 需 ≥ 0。")
    if args.retries < 0:
        return usage_error("--retries 需 ≥ 0。")
    if args.timeout <= 0:
        return usage_error("--timeout 需 > 0。")

    try:
        time_range = _validate_time_range(args.time_range)
    except ValueError as exc:
        return usage_error(str(exc))

    api_key = resolve_api_key(args.api_key)
    ak = sk = session_token = None
    if not api_key:
        ak, sk, session_token = get_credentials()
        if not ak or not sk:
            print(
                "Error: 未找到凭证。请选择一种方式配置：\n"
                "1) 环境变量 WEB_SEARCH_API_KEY，或传 --api-key\n"
                "2) 技能目录或 ~/.openclaw/.env 写入 WEB_SEARCH_API_KEY=...\n"
                "3) AK/SK：环境变量 VOLCENGINE_ACCESS_KEY 和 VOLCENGINE_SECRET_KEY\n"
                "开通指南：references/setup-guide.md",
                file=sys.stderr,
            )
            return 1

    body = build_body(
        query=args.query,
        search_type=args.type,
        count=args.count,
        time_range=time_range,
        auth_level=args.auth_level,
        query_rewrite=args.query_rewrite,
    )

    try:
        data = do_search(
            body,
            api_key=api_key,
            ak=ak,
            sk=sk,
            session_token=session_token or "",
            timeout=args.timeout,
            retries=args.retries,
        )
    except SearchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if exc.status == 429:
            print("提示：请求频率过高触发限流，建议降频后重试。详见 references/setup-guide.md", file=sys.stderr)
        elif exc.status == 401 or "InvalidAccessKey" in exc.body:
            print(
                "提示：凭证无效或已失效。请检查 WEB_SEARCH_API_KEY，"
                "或 VOLCENGINE_ACCESS_KEY / VOLCENGINE_SECRET_KEY。",
                file=sys.stderr,
            )
        elif exc.body:
            print(exc.body, file=sys.stderr)
        return 1

    if data is None:
        print("Error: 接口未返回内容。", file=sys.stderr)
        return 1

    error = (data.get("ResponseMetadata") or {}).get("Error")
    if error:
        code = str(error.get("Code", ""))
        message = error.get("Message", "")
        print(f"API Error [{code}]: {message}", file=sys.stderr)
        hint = ERROR_HINTS.get(code)
        if hint:
            print(hint, file=sys.stderr)
        elif "invalid_api_key" in code.lower() or "10403" in code:
            print(
                "提示：请确认 API Key 来自联网搜索控制台 https://console.volcengine.com/search-infinity/api-key ，"
                "而非火山方舟(Ark)。",
                file=sys.stderr,
            )
        elif "429" in code or "flowlimit" in code.lower():
            print("提示：请求频率过高触发限流，建议降频后重试。", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print(format_text(data, args.type, args.max_chars))
    return 0


if __name__ == "__main__":
    # Windows consoles and pipes default to a legacy code page; results are Chinese text.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
