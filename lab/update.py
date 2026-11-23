"""更新检查：只在用户点击之后访问配置的 GitHub Release 仓，只读，不上传本机数据。

**三层边界**（与 `SECURITY.md` 的新口径同版）：

1. 本地观测、回执、采集与界面核心**不主动联网**——本模块只被 `POST /about/check-update`
   与托盘「检查更新」调用，启动路径与 `create_app()` 一次都不碰它（钉这条）。
2. 只允许 `GET`、请求体恒空、超时固定 10 秒、不跟随到非 GitHub 主机。
3. 不发工作区路径、用户名、会话 id、Vault 内容、设备标识，也不读任何凭据。

仓库标识 `RELEASE_REPO` 住在这里而**不住 `lab/contract.py`**：contract.py 是命名与兼容契约表，
更新源没有 legacy 可保；仓里另有一个 repo 语义是主题卡的 `org_path`（谁和谁同一主题），
两者并排住迟早被读错。这里只留一个消费者需要它。默认 `rultrix/groma`（D1 于 2026-11-19 兑现：
`releases/latest` 现查 v0.54.0 正式版、产品自跑 `check_latest()` 得 `up_to_date` @02:33:26Z），
`GROMA_RELEASE_REPO` 可覆盖。**没现查过接通的仓库不许写成默认**。

`checked_at` 存 UTC（W23 定的存储口径），界面用 `local_ts` 显本机时刻。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from lab import __version__
from lab.contract import env_value
from lab.workspace import utc_stamp

RELEASE_REPO = env_value("GROMA_RELEASE_REPO", default="rultrix/groma")
API_ROOT = "https://api.github.com/repos/"
TIMEOUT_SECONDS = 10
STATE_FILE = "update-check.json"
ASSET_BASE = "groma-"
ASSET_SUFFIX = "-windows-x64.zip"
CHECKSUM_ASSET = "SHA256SUMS.txt"

STATES = ("up_to_date", "update_available", "prerelease_available", "not_configured",
          "network_error", "bad_response")

_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_OFFSITE = re.compile(r"^[a-z]+://(api\.)?github\.com(:443)?/", re.I)


def _open(request: urllib.request.Request):
    """唯一的出网点：测试把这一层换掉，就能拿到真正的 Request 对象来验方法/header/body。"""
    opener = urllib.request.build_opener(_GitHubOnly())
    return opener.open(request, timeout=TIMEOUT_SECONDS)


class _GitHubOnly(urllib.request.HTTPRedirectHandler):
    """只跟 GitHub 自己的跳转；跳到别的 host 当作失败，不悄悄放行。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        if not _OFFSITE.match(newurl):
            raise urllib.error.HTTPError(newurl, code, "重定向离开 GitHub：%s" % newurl, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def parse_tag(tag: object) -> tuple[int, int, int] | None:
    """`0.54.0` 与 `v0.54.0` 都认；其余（含预发布后缀）返回 None，由调用方分类处理。"""
    match = _TAG.match(str(tag or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def request_for(repo: str) -> urllib.request.Request:
    url = API_ROOT + repo.strip("/") + "/releases/latest"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "Groma/%s" % __version__)
    return request


def release_url(url: str) -> str:
    """把 api 地址换成人在浏览器里看的那条网页地址。"""
    return url.replace("api.github.com/repos/", "github.com/") + "/latest"


def asset_names(payload: dict) -> list[str]:
    return [str(item.get("name")) for item in payload.get("assets") or [] if item.get("name")]


def record(repo: str | None = None, *, state: str, current: str | None = None,
           latest: str | None = None, page: str | None = None, asset: str | None = None,
           checksum: str | None = None, error: str | None = None,
           checked_at: str | None = None) -> dict:
    if state not in STATES:
        raise ValueError("未知更新状态：%s" % state)
    return {
        "schema": 1,
        "checked_at": checked_at or utc_stamp(),
        "repo": repo if repo else None,
        "current": current or __version__,
        "state": state,
        "latest": latest,
        "release_url": page,
        "asset": asset,
        "checksum_asset": checksum,
        "error": error,
    }


def check_latest(repo: str | None = None, current: str | None = None,
                 fetch: Callable[[urllib.request.Request], bytes] | None = None) -> dict:
    """查一次最新发布版。返回状态记录，不抛异常——失败也要变成看得见的状态。"""
    target = repo if repo is not None else RELEASE_REPO
    base = current or __version__
    if not target or "/" not in target:
        return record(target, state="not_configured", current=base,
                      error="更新仓库尚未配置（GROMA_RELEASE_REPO 为空）")

    loader = fetch or _read
    try:
        body = loader(request_for(target))
    except (urllib.error.URLError, OSError) as exc:
        return record(target, state="network_error", current=base, error=str(exc))
    try:
        payload = json.loads(body)
    except ValueError as exc:
        return record(target, state="bad_response", current=base,
                      error="返回不是合法 JSON：%s" % exc)
    if not isinstance(payload, dict):
        return record(target, state="bad_response", current=base, error="返回不是 JSON 对象")

    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        return record(target, state="bad_response", current=base, error="返回里没有 tag_name")
    page = str(payload.get("html_url") or release_url(API_ROOT + target + "/releases/latest"))
    names = asset_names(payload)
    zip_name = next((name for name in names if name.endswith(ASSET_SUFFIX)), None)
    checksum = CHECKSUM_ASSET if CHECKSUM_ASSET in names else None
    found = record(target, state="up_to_date", current=base, latest=tag, page=page,
                   asset=zip_name, checksum=checksum)
    if parse_tag(tag) is None:
        found["state"] = "bad_response"
        found["error"] = "无法解析的版本号：%s" % tag
        found["latest"] = None
        return found
    if payload.get("prerelease") is True:
        # 预发布不自动推荐安装（§8.4），哪怕号比本机大
        found["state"] = "prerelease_available"
        return found

    running = parse_tag(base)
    if running is None:
        found["state"] = "bad_response"
        found["error"] = "本机版本号无法解析：%s" % base
        found["latest"] = None
        return found
    found["state"] = "update_available" if parse_tag(tag) > running else "up_to_date"
    return found


def _read(request: urllib.request.Request) -> str:
    with _open(request) as response:
        return response.read().decode("utf-8", "replace")


def save(root: Path, result: dict) -> Path:
    from lab.state import state_root

    path = state_root() / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load() -> dict | None:
    from lab.state import state_root

    path = state_root() / STATE_FILE
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


NOT_CHECKED = "尚未检查更新"
STATUS_TEXT = {
    "up_to_date": "已是最新（{latest}）",
    "update_available": "有新版本 {latest}（本机 {current}）",
    "prerelease_available": "最新是一个预发布版本 {latest}，不自动推荐安装",
    "not_configured": "更新仓库尚未配置",
    "network_error": "最近一次检查失败：{error}",
    "bad_response": "最近一次检查失败：{error}",
}


def describe(status: dict | None) -> str:
    """把状态记录讲成人话。失败态**不许**说成"已是最新"——空、错、未配置是三件不同的事。"""
    if not status:
        return NOT_CHECKED
    template = STATUS_TEXT.get(str(status.get("state")))
    if template is None:
        return "最近一次检查失败：无法识别的状态 %s" % status.get("state")
    return template.format(latest=status.get("latest") or "（未给出）",
                           current=status.get("current") or __version__,
                           error=status.get("error") or "未给出原因")
