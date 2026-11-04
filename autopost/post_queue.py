"""投稿キューを処理する本体（GitHub Actions から30分ごとに呼ばれる）。

流れ：Notionの「投稿予約」かつ予定日時を過ぎた行 → 台本JSONから動画を作る → 自動チェック
     → （本番のときだけ）一時置き場にアップ → Instagramに公開 → Notionに結果を書く。

安全装置：
- DRY_RUN（既定オン）の間は投稿しない。動画を作ってチェックするだけ。
- 1日の投稿上限（MAX_POSTS_PER_DAY、既定1）。
- 自動チェック不合格・投稿文が長すぎる・ハッシュタグ6個以上 → 投稿せず「エラー」。
- 予定日時を48時間以上過ぎた行は投稿せず「期限切れ」（古い話題を出さない）。
- 公開の呼び出しは1回だけ。失敗したら「エラー」にして人が確認する（二重投稿を防ぐ）。
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from . import hosting
from .common import ACCOUNTS, JST, Config, account_key, log, now_jst
from .instagram import Instagram
from .notion import (Notion, p_date, p_select, p_text, p_title, w_date, w_select, w_status, w_text,
                     w_title)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "render"))
import render_reel  # noqa: E402

DRY_MARK = "DRY_RUN確認済み"
CAPTION_MAX = 2200
OVERDUE = timedelta(hours=48)


def _parse(dt: str) -> datetime:
    d = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if d.tzinfo is None:                       # 日付だけ・時刻だけの入力は日本時間として扱う
        d = d.replace(tzinfo=JST)
    return d


def posted_today(notion: Notion, db: str, account: str = "main") -> int:
    """今日このアカウントで投稿済みの本数（1日の上限はアカウントごと）。"""
    today = now_jst().date().isoformat()
    rows = notion.query(db, {"and": [
        {"property": "ステータス", "status": {"equals": "投稿済み"}},
        {"property": "投稿日時", "date": {"on_or_after": today + "T00:00:00+09:00"}}]})
    return sum(1 for r in rows if account_key(r) == account)


def _client(ig, key: str):
    """ig は Instagram（事務だけ）か {キー: Instagram}。そのアカウントの投稿用クライアントを返す。"""
    if isinstance(ig, dict):
        return ig.get(key)
    return ig if key == "main" else None


def due_rows(notion: Notion, db: str) -> list[dict]:
    now = now_jst().isoformat()
    return notion.query(db, {"and": [
        {"property": "ステータス", "status": {"equals": "投稿予約"}},
        {"property": "予定日時", "date": {"on_or_before": now}}]},
        sorts=[{"property": "予定日時", "direction": "ascending"}])


# Excelのエラー表示（#N/A など）はハッシュタグとして数えない
_EXCEL_ERR = re.compile(r"[#＃](N/A|REF!|VALUE!|DIV/0!|NAME\?|NUM!|NULL!|SPILL!|CALC!|GETTING_DATA|ERROR!)")
_TAG = re.compile(r"(?:^|\s)[#＃][^\s#＃]+")


def count_hashtags(text: str) -> int:
    return len(_TAG.findall(_EXCEL_ERR.sub("", text)))


def check_post(spec: dict, qa: dict) -> tuple[list[str], str]:
    """投稿してよいかの最終チェック。問題のリストと投稿文を返す。"""
    problems = []
    if not qa.get("passed_format_checks"):
        problems.append("自動チェック不合格：" + " / ".join(qa.get("warnings", []))[:600])
    if len(spec.get("hashtags", [])) > 5:
        problems.append("ハッシュタグが6個以上（Instagramの上限は5個）")
    if "post" not in spec:
        problems.append("台本に post（表紙の文字・代替テキスト）がありません")
    caption = render_reel.build_caption(spec)
    if count_hashtags(caption) > 5:
        problems.append(f"投稿文全体のハッシュタグが{count_hashtags(caption)}個（本文中の#も数えて5個まで）")
    if len(caption) > CAPTION_MAX:
        problems.append(f"投稿文が{len(caption)}字（上限{CAPTION_MAX}字）")
    if not 3 <= qa.get("duration_seconds", 0) <= 90:
        problems.append(f"動画の長さが{qa.get('duration_seconds')}秒（3〜90秒にしてください）")
    return problems, caption


CHECK_MSG = ("Instagramへの公開の操作をした後に問題が起きました。公開されている可能性があります。"
             "必ずInstagramのプロフィールで公開済みか確認し、公開済みなら「投稿済み」にしてリールURLを入れてください。"
             "公開されていない場合だけ「投稿予約」に戻してください。詳細：")


def _safe_update(notion: Notion, pid: str, props: dict, what: str) -> bool:
    try:
        notion.update(pid, props)
        return True
    except Exception as e:
        log(f"Notionの更新に失敗（{what}）：{e}")
        return False


def process(cfg: Config, notion: Notion, ig: Instagram | None, workdir: Path,
            render=render_reel.render, host=hosting) -> dict:
    summary = {"posted": [], "dry_run": [], "errors": [], "expired": [], "needs_check": [], "skipped": 0}
    rows = due_rows(notion, cfg.queue_db)
    if not rows:
        log("予定時刻を過ぎた「投稿予約」はありません")
        return summary
    remaining_by: dict[str, int] = {}

    def left(key: str) -> int:
        if key not in remaining_by:
            remaining_by[key] = cfg.max_posts_per_day - posted_today(notion, cfg.queue_db, key)
        return remaining_by[key]

    log(f"対象 {len(rows)} 件／1日の上限：アカウントごとに {cfg.max_posts_per_day} 本（DRY_RUN={'オン' if cfg.dry_run else 'オフ'}）")

    for page in rows:
        pid, name = page["id"], p_title(page, "投稿名") or page["id"]
        acct = account_key(page)
        label = ACCOUNTS[acct][0]
        remaining = left(acct)
        if cfg.dry_run and p_text(page, "エラー内容").startswith(DRY_MARK):
            summary["skipped"] += 1
            continue
        stage = "準備"
        try:
            if now_jst() - _parse(p_date(page, "予定日時")) > OVERDUE:
                msg = "予定日時を48時間以上過ぎたため投稿しませんでした。日時を直して「投稿予約」に戻すと再開します。"
                if cfg.dry_run:                                     # お試し中はステータスを変えない
                    notion.update(pid, {"エラー内容": w_text(f"{DRY_MARK}（期限切れ）：本番なら投稿されません。" + msg)})
                else:
                    notion.update(pid, {"ステータス": w_status("期限切れ"), "エラー内容": w_text(msg)})
                    summary["expired"].append(name)
                continue
            if remaining <= 0 and not cfg.dry_run:
                log(f"{label}アカウントは今日の上限に達したため、明日に回します：{name}")
                summary["skipped"] += 1
                continue
            client = None if cfg.dry_run else _client(ig, acct)
            if not cfg.dry_run and client is None:
                raise RuntimeError(f"{label}アカウントの投稿設定（{ACCOUNTS[acct][1]} / {ACCOUNTS[acct][2]}）がGitHubのSecretsにありません")

            if not cfg.dry_run:
                notion.update(pid, {"ステータス": w_status("投稿中")})     # 二重処理の防止
            spec = notion.script_json(pid)
            out = workdir / (spec.get("id") or pid)
            qa = render(_write_spec(spec, workdir), out)
            problems, caption = check_post(spec, qa)
            if problems:
                raise ValueError("投稿前チェックで停止：" + "／".join(problems))

            if cfg.dry_run:
                notion.update(pid, {"エラー内容": w_text(
                    f"{DRY_MARK}（{now_jst():%m/%d %H:%M}）：動画{qa['duration_seconds']}秒・投稿文{len(caption)}字。"
                    "本番にするとこのまま投稿されます。動画はActionsの実行結果からダウンロードして確認できます。"),
                    "動画の長さ_秒": {"number": qa["duration_seconds"]}})
                summary["dry_run"].append(name)
                continue

            if client.quota_used() >= 50:
                raise RuntimeError("24時間の投稿数が50件以上。異常の可能性があるため停止しました")
            base = f"{spec.get('id', 'reel')}_{now_jst():%Y%m%d%H%M%S}"     # 毎回別名（CDNの古いキャッシュ対策）
            cover_jpg = _to_jpeg(out / "cover.png")                  # 表紙はJPEGのみ受け付けられる
            try:
                urls = host.upload(cfg.repo, {f"{base}.mp4": out / "reel.mp4", f"{base}_cover.jpg": cover_jpg})
                host.wait_public(list(urls.values()))
                video_url = next(u for k, u in urls.items() if k.endswith(".mp4"))
                cover_url = next(u for k, u in urls.items() if k.endswith(".jpg"))
                container = client.prepare_reel(video_url, caption, cover_url)  # ここまでの失敗＝未公開
                stage = "公開"
                media_id = client.publish(container)                     # 1回だけ。再試行しない
                stage = "公開済み"
            finally:
                try:
                    host.clear(cfg.repo)
                except Exception as e:                                # 片付け失敗は投稿の成否に影響させない
                    log(f"一時置き場の片付けに失敗（次回の投稿で上書きされます）：{e}")
        except Exception as e:  # 1件の失敗で他の行を止めない
            log(f"失敗（{stage}）：{name}：{e}")
            traceback.print_exc()
            if stage == "公開":
                _safe_update(notion, pid, {"ステータス": w_status("要確認"), "エラー内容": w_text(CHECK_MSG + str(e))}, "要確認")
                summary["needs_check"].append({"name": name, "error": str(e)[:300]})
            elif cfg.dry_run:
                _safe_update(notion, pid, {"エラー内容": w_text("DRY_RUNで見つかった問題（本番なら投稿されません）：" + str(e))}, "お試しの結果")
                summary["errors"].append({"name": name, "error": str(e)[:300]})
            else:
                _safe_update(notion, pid, {"ステータス": w_status("エラー"), "エラー内容": w_text(str(e))}, "エラー")
                summary["errors"].append({"name": name, "error": str(e)[:300]})
            continue

        # ---- 公開に成功した後：ここから先の失敗では、ステータスを「エラー」にしない（再投稿の誘発を防ぐ）
        remaining_by[acct] = remaining - 1
        stamp = now_jst().isoformat(timespec="seconds")
        ok = _safe_update(notion, pid, {"ステータス": w_status("投稿済み"), "メディアID": w_text(media_id),
                                        "投稿日時": w_date(stamp), "動画の長さ_秒": {"number": qa["duration_seconds"]},
                                        "エラー内容": {"rich_text": []}}, "投稿済み")
        link = ""
        try:
            link = client.permalink(media_id)
            if link:
                _safe_update(notion, pid, {"投稿URL": {"url": link}}, "投稿URL")
        except Exception as e:
            log(f"投稿URLの取得に失敗（投稿は成功）：{e}")
        try:
            props = {"投稿名": w_title(name), "投稿日": w_date(stamp[:10]), "メディアID": w_text(media_id),
                     "動画の長さ_秒": {"number": qa["duration_seconds"]}, "取得方法": w_select("API")}
            if link:
                props["リールURL"] = {"url": link}
            if p_select(page, "テーマ"):
                props["テーマ"] = w_select(p_select(page, "テーマ"))
            props["アカウント"] = w_select(label)
            notion.create(cfg.results_db, props)
        except Exception as e:
            log(f"リール実績DBへの記録に失敗（投稿は成功）：{e}")
        if not ok:
            summary["needs_check"].append({"name": name, "error": f"投稿は成功（メディアID {media_id}）。キューの更新に失敗したので手で「投稿済み」にしてください"})
        summary["posted"].append({"name": name, "url": link, "media_id": media_id})
        log(f"投稿しました：{name} {link}")
    return summary


def _to_jpeg(png: Path) -> Path:
    from PIL import Image
    jpg = png.with_suffix(".jpg")
    if png.exists():
        Image.open(png).convert("RGB").save(jpg, "JPEG", quality=92)
    return jpg


def _write_spec(spec: dict, workdir: Path) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    p = workdir / f"{spec.get('id', 'reel')}.json"
    p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return p


def main() -> int:
    cfg = Config.from_env()
    if not cfg.notion_token:
        log("NOTION_TOKEN が未設定です（セットアップ手順書の手順3）")
        return 1
    notion = Notion(cfg.notion_token)
    ig = {}
    if not cfg.dry_run:
        for key, (tok, uid) in cfg.accounts.items():
            if not tok:
                continue                      # 未設定のアカウントの行は「エラー」で知らせる
            try:
                c = Instagram(tok, uid, cfg.graph_version)
                c.resolve_user_id()
                ig[key] = c
            except Exception as e:
                log(f"{ACCOUNTS[key][0]}アカウントのトークンが使えません：{e}")
        if not ig:
            log("本番投稿に必要な設定がありません：IG_ACCESS_TOKEN（または IG_ACCESS_TOKEN_KANJI）")
            return 1
    summary = process(cfg, notion, ig, ROOT / "out")
    Path(ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out" / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log("結果：" + json.dumps(summary, ensure_ascii=False))
    return 1 if summary["errors"] or summary["expired"] or summary["needs_check"] else 0


if __name__ == "__main__":
    sys.exit(main())
