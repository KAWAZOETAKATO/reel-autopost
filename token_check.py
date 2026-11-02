"""Instagramトークンの確認（毎週）。

Facebookログイン版では「期限なしのページアクセストークン」を使うので、延長は不要。
毎週、トークンが使えるか・期限なしのままかを確かめ、だめなら失敗させて通知する。
トークンの中身はどこにも表示しない。
"""
from __future__ import annotations

import sys
from datetime import datetime

from .common import JST, Config, HttpError, log
from .instagram import Instagram, debug_token


def main() -> int:
    cfg = Config.from_env()
    if not cfg.ig_token:
        log("IG_ACCESS_TOKEN が未設定です")
        return 1
    try:
        me = Instagram(cfg.ig_token, cfg.ig_user_id, cfg.graph_version).me()
        log(f"トークンは有効です（アカウント：@{me.get('username', '?')}／ID：{me.get('user_id', '?')}）")
    except (HttpError, RuntimeError) as e:
        log(f"トークンが使えません（{getattr(e, 'status', '')} {str(e)[:200]}）。手順書の手順3〜4でトークンを作り直してください")
        return 1

    try:
        info = debug_token(cfg.ig_token, cfg.graph_version)
    except HttpError as e:
        log(f"トークンの期限を調べられませんでした（HTTP {e.status}）。投稿には影響しません")
        return 0
    kind, exp = info.get("type", "?"), int(info.get("expires_at") or 0)
    if kind != "PAGE":
        log(f"トークンの種類が {kind} です。ページアクセストークン（手順書の手順4）に入れ替えてください")
        return 1
    if exp:
        days = (datetime.fromtimestamp(exp, JST) - datetime.now(JST)).days
        log(f"このトークンには期限があります（残り約{days}日）。手順書の手順4で期限なしのトークンを作り直してください")
        return 1
    log("ページアクセストークン・期限なし。延長は不要です")
    return 0


if __name__ == "__main__":
    sys.exit(main())
