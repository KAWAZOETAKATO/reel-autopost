"""共通部品：設定の読み込み・HTTP・ログ。

秘密情報（トークン類）は環境変数（GitHub Secrets）からだけ読み、絶対にログへ出さない。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# Notion のデータベースID（AI運用会社のページの下にあるもの）
QUEUE_DB_DEFAULT = "d3e140c4ec8e49ce834f91925be9e0fc"     # リール投稿キュー（インスタ部）
RESULTS_DB_DEFAULT = "94a0a494829e478b8833120c6a1f4963"   # リール実績DB（インスタ部）
ACCOUNT_DB_DEFAULT = "70e14b9fd5634fe1b50595c9c7eff3a2"   # アカウント推移DB（インスタ）


def now_jst() -> datetime:
    return datetime.now(JST)


def log(msg: str) -> None:
    print(f"[{now_jst():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def _dry_run(v: str | None) -> bool:
    """本番（False）になるのは、値がはっきり false / 0 / no / off のときだけ。打ち間違いはお試しのまま。"""
    return (v or "").strip().lower() not in ("false", "0", "no", "off")


@dataclass
class Config:
    notion_token: str = ""
    ig_token: str = ""
    ig_user_id: str = ""
    queue_db: str = QUEUE_DB_DEFAULT
    results_db: str = RESULTS_DB_DEFAULT
    account_db: str = ACCOUNT_DB_DEFAULT
    dry_run: bool = True                 # 既定は「投稿しない」。リポジトリ変数 DRY_RUN=false で本番
    max_posts_per_day: int = 1           # 1日の自動投稿の上限（APIの上限は24時間で100件）
    graph_version: str = "v26.0"
    repo: str = ""                       # owner/repo（動画の一時置き場URLに使う）
    secrets_missing: list = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Config":
        c = cls(
            notion_token=os.environ.get("NOTION_TOKEN", ""),
            ig_token=os.environ.get("IG_ACCESS_TOKEN", ""),
            ig_user_id=os.environ.get("IG_USER_ID") or "me",     # 未設定なら「トークンの持ち主」で投稿
            queue_db=os.environ.get("NOTION_QUEUE_DB") or QUEUE_DB_DEFAULT,
            results_db=os.environ.get("NOTION_RESULTS_DB") or RESULTS_DB_DEFAULT,
            account_db=os.environ.get("NOTION_ACCOUNT_DB") or ACCOUNT_DB_DEFAULT,
            dry_run=_dry_run(os.environ.get("DRY_RUN")),
            max_posts_per_day=int(os.environ.get("MAX_POSTS_PER_DAY") or 1),
            graph_version=os.environ.get("IG_GRAPH_VERSION") or "v26.0",
            repo=os.environ.get("GITHUB_REPOSITORY", ""),
        )
        for name, val in (("NOTION_TOKEN", c.notion_token), ("IG_ACCESS_TOKEN", c.ig_token)):
            if not val:
                c.secrets_missing.append(name)
        return c


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        # URLのクエリ（access_token を含みうる）はエラー文に残さない
        safe_url = url.split("?")[0]
        super().__init__(f"HTTP {status} {safe_url}: {body[:500]}")
        self.status = status
        self.body = body


def http_json(method: str, url: str, *, headers: dict | None = None, params: dict | None = None,
              body: dict | None = None, form: dict | None = None, timeout: int = 60, retries: int = 2) -> dict:
    """JSONを返すHTTP呼び出し。5xxと429だけ最大 retries 回まで待って再試行する。"""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8") or "{}"
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if (e.code >= 500 or e.code == 429) and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise HttpError(e.code, text, url) from None


def fail(msg: str, code: int = 1) -> None:
    log("エラー: " + msg)
    sys.exit(code)
