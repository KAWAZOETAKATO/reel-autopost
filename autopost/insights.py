"""リール実績DBの数字を、Instagram APIから毎日自動で更新する（スクショ不要）。

対象：メディアIDが入っていて、投稿から30日以内の行。
"""
from __future__ import annotations

import sys
from datetime import timedelta

from .common import ACCOUNTS, Config, HttpError, account_key, log, now_jst
from .instagram import Instagram
from .notion import Notion, p_text, p_title, w_date, w_select, w_title

# APIの指標名 → リール実績DBの列名
MAP = {"views": "再生数", "reach": "リーチ", "likes": "いいね", "comments": "コメント",
       "shares": "シェア", "saved": "保存"}


def to_props(m: dict) -> dict:
    props = {col: {"number": m[k]} for k, col in MAP.items() if k in m}
    if "ig_reels_avg_watch_time" in m:                        # ミリ秒 → 秒
        props["平均視聴時間_秒"] = {"number": round(m["ig_reels_avg_watch_time"] / 1000, 1)}
    return props


def record_account(cfg: Config, notion: Notion, ig: Instagram) -> dict | None:
    """アカウント推移DBに今日の1行を書く（同じ日の行があれば上書き）。失敗しても数字の更新は続ける。"""
    try:
        a = ig.account()
        today = now_jst().date().isoformat()
        name = "@" + a.get("username", "")
        props = {"記録": w_title(f"{today} {name}"), "日付": w_date(today), "アカウント": w_select(name),
                 "取得方法": w_select("API")}
        if a.get("followers_count") is not None:
            props["フォロワー数"] = {"number": a["followers_count"]}
        if a.get("media_count") is not None:
            props["投稿数"] = {"number": a["media_count"]}
        same = notion.query(cfg.account_db, {"and": [
            {"property": "日付", "date": {"equals": today}},
            {"property": "アカウント", "select": {"equals": name}}]})
        if same:
            notion.update(same[0]["id"], props)
        else:
            notion.create(cfg.account_db, props)
        log(f"アカウントの数字：{name} フォロワー{a.get('followers_count')}・投稿{a.get('media_count')}")
        return a
    except Exception as e:
        log(f"アカウントの数字の記録に失敗：{e}")
        return None


def run(cfg: Config, notion: Notion, ig) -> dict:
    """ig は Instagram（事務だけ）か {キー: Instagram}。"""
    clients = ig if isinstance(ig, dict) else {"main": ig}
    for c in clients.values():
        record_account(cfg, notion, c)
    since = (now_jst() - timedelta(days=30)).date().isoformat()
    rows = notion.query(cfg.results_db, {"and": [
        {"property": "メディアID", "rich_text": {"is_not_empty": True}},
        {"property": "投稿日", "date": {"on_or_after": since}}]})
    done, failed = 0, []
    for page in rows:
        mid = p_text(page, "メディアID").strip()
        client = clients.get(account_key(page))
        if client is None:
            continue
        try:
            m = client.insights(mid)
        except HttpError as e:
            failed.append(f"{p_title(page, '投稿名')}（{e.status}）")
            continue
        props = to_props(m)
        if not props:
            continue
        props["取得方法"] = w_select("API")
        props["読取日"] = w_date(now_jst().date().isoformat())
        notion.update(page["id"], props)
        done += 1
    log(f"数字を更新：{done}件／失敗：{len(failed)}件 {failed}")
    return {"updated": done, "failed": failed}


def main() -> int:
    cfg = Config.from_env()
    if cfg.secrets_missing:
        log("設定がありません：" + ", ".join(cfg.secrets_missing))
        return 1
    clients = {}
    for key, (tok, uid) in cfg.accounts.items():
        if not tok:
            continue
        try:
            c = Instagram(tok, uid, cfg.graph_version)
            c.resolve_user_id()
            clients[key] = c
        except Exception as e:
            log(f"{ACCOUNTS[key][0]}アカウントのトークンが使えません：{e}")
    if not clients:
        return 1
    r = run(cfg, Notion(cfg.notion_token), clients)
    return 1 if r["failed"] and not r["updated"] else 0


if __name__ == "__main__":
    sys.exit(main())
