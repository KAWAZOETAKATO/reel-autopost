"""ネットにつながずに動作を確認するテスト（Notion・Instagram・一時置き場はすべて偽物）。

実行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopost import insights, post_queue  # noqa: E402
from autopost.common import Config, HttpError, now_jst  # noqa: E402
from autopost.instagram import Instagram  # noqa: E402

SPEC = json.loads((ROOT / "examples" / "reel01.json").read_text(encoding="utf-8"))
GOOD_QA = {"passed_format_checks": True, "duration_seconds": 29.4, "warnings": []}


def page(pid, name, status="投稿予約", due=None, theme="関数", err="", account=None):
    due = due or (now_jst() - timedelta(minutes=5)).isoformat()
    extra = {"アカウント": {"select": {"name": account}}} if account else {}
    return {"id": pid, "properties": {**extra,
        "投稿名": {"title": [{"plain_text": name}]},
        "ステータス": {"status": {"name": status}},
        "予定日時": {"date": {"start": due}},
        "テーマ": {"select": {"name": theme}},
        "エラー内容": {"rich_text": [{"plain_text": err}] if err else []},
    }}


class FakeNotion:
    def __init__(self, rows, posted_today=0, spec=SPEC, fail_status=None):
        self.rows, self._posted, self.spec, self.fail_status = rows, posted_today, spec, fail_status
        self.updates, self.created = [], []

    def query(self, db, filter_=None, sorts=None):
        conds = filter_["and"]
        if conds[0]["status"]["equals"] == "投稿済み":
            return [{}] * self._posted
        return [r for r in self.rows if r["properties"]["ステータス"]["status"]["name"] == "投稿予約"]

    def update(self, pid, props):
        st = props.get("ステータス", {}).get("status", {}).get("name")
        if self.fail_status and st == self.fail_status:
            raise HttpError(400, "invalid status option", "https://api.notion.com/v1/pages/x")
        self.updates.append((pid, props))

    def create(self, db, props):
        self.created.append((db, props))

    def script_json(self, pid):
        return self.spec

    def last_status(self, pid):
        for p, props in reversed(self.updates):
            if p == pid and "ステータス" in props:
                return props["ステータス"]["status"]["name"]


class FakeIG:
    def __init__(self, fail=False, quota=0, publish_fail=False, permalink_fail=False):
        self.fail, self.quota, self.publish_fail, self.permalink_fail = fail, quota, publish_fail, permalink_fail
        self.published, self.publish_calls = [], 0

    def quota_used(self):
        return self.quota

    def prepare_reel(self, video_url, caption, cover_url=None):
        if self.fail:
            raise RuntimeError("Instagram側の動画処理に失敗：ERROR")
        self._pending = (video_url, caption, cover_url)
        return "C1"

    def publish(self, container):
        self.publish_calls += 1
        if self.publish_fail:
            raise HttpError(500, "server error", "https://graph.instagram.com/v26.0/1/media_publish")
        self.published.append(self._pending)
        return "17900000000000001"

    def permalink(self, mid):
        if self.permalink_fail:
            raise HttpError(500, "x", "https://graph.instagram.com/x")
        return "https://www.instagram.com/reel/TEST/"


class FakeHost:
    def __init__(self):
        self.uploaded, self.cleared = [], 0

    def upload(self, repo, files):
        self.uploaded.append(files)
        return {k: f"https://raw.githubusercontent.com/{repo}/media/{k}" for k in files}

    def clear(self, repo):
        self.cleared += 1

    def wait_public(self, urls):
        self.waited = list(urls)


def fake_render(qa=GOOD_QA):
    def r(spec_path, out):
        out.mkdir(parents=True, exist_ok=True)
        (out / "reel.mp4").write_bytes(b"x")
        from PIL import Image
        Image.new("RGB", (1080, 1920), "white").save(out / "cover.png")
        return qa
    return r


def cfg(dry=True, limit=1):
    return Config(notion_token="n", ig_token="t", ig_user_id="1", dry_run=dry, max_posts_per_day=limit, repo="me/reels")


class PostQueueTest(unittest.TestCase):
    def run_q(self, c, notion, ig=None, qa=GOOD_QA):
        host = FakeHost()
        with tempfile.TemporaryDirectory() as d:
            s = post_queue.process(c, notion, ig, Path(d), render=fake_render(qa), host=host)
        return s, host

    def test_dry_run_never_posts(self):
        n, ig = FakeNotion([page("a", "#01")]), FakeIG()
        s, host = self.run_q(cfg(dry=True), n, ig)
        self.assertEqual(s["dry_run"], ["#01"])
        self.assertEqual(ig.published, [])
        self.assertEqual(host.uploaded, [])
        self.assertIsNone(n.last_status("a"))                     # ステータスは変えない
        self.assertTrue(n.updates[-1][1]["エラー内容"]["rich_text"][0]["text"]["content"].startswith(post_queue.DRY_MARK))

    def test_dry_run_skips_already_checked(self):
        n = FakeNotion([page("a", "#01", err=post_queue.DRY_MARK + "（済）")])
        s, _ = self.run_q(cfg(dry=True), n)
        self.assertEqual(s["skipped"], 1)
        self.assertEqual(n.updates, [])

    def test_live_post_writes_back(self):
        n, ig = FakeNotion([page("a", "#01")]), FakeIG()
        s, host = self.run_q(cfg(dry=False), n, ig)
        self.assertEqual(len(ig.published), 1)
        video, caption, cover = ig.published[0]
        self.assertTrue(video.endswith(".mp4") and cover.endswith("_cover.jpg"))
        self.assertIn("@aiaiai4772", caption)                       # 共通の締め文が付く
        self.assertEqual(n.last_status("a"), "投稿済み")
        self.assertEqual(host.cleared, 1)                           # 一時置き場を片付けた
        db, props = n.created[0]
        self.assertEqual(props["メディアID"]["rich_text"][0]["text"]["content"], "17900000000000001")
        self.assertEqual(props["テーマ"]["select"]["name"], "関数")

    def test_daily_limit(self):
        n, ig = FakeNotion([page("a", "#01"), page("b", "#02")]), FakeIG()
        s, _ = self.run_q(cfg(dry=False, limit=1), n, ig)
        self.assertEqual(len(ig.published), 1)
        self.assertEqual(s["skipped"], 1)
        n2, ig2 = FakeNotion([page("a", "#01")], posted_today=1), FakeIG()
        self.run_q(cfg(dry=False, limit=1), n2, ig2)
        self.assertEqual(ig2.published, [])

    def test_qa_failure_blocks_post(self):
        n, ig = FakeNotion([page("a", "#01")]), FakeIG()
        bad = {"passed_format_checks": False, "duration_seconds": 20, "warnings": ["文字が安全領域の外"]}
        s, host = self.run_q(cfg(dry=False), n, ig, qa=bad)
        self.assertEqual(ig.published, [])
        self.assertEqual(host.uploaded, [])
        self.assertEqual(n.last_status("a"), "エラー")
        self.assertIn("安全領域", s["errors"][0]["error"])

    def test_too_many_hashtags_blocks(self):
        spec = dict(SPEC, hashtags=["#a", "#b", "#c", "#d", "#e", "#f"])
        n, ig = FakeNotion([page("a", "#01")], spec=spec), FakeIG()
        s, _ = self.run_q(cfg(dry=False), n, ig)
        self.assertEqual(ig.published, [])
        self.assertIn("ハッシュタグ", s["errors"][0]["error"])

    def test_overdue_expires(self):
        old = (now_jst() - timedelta(hours=60)).isoformat()
        n, ig = FakeNotion([page("a", "#01", due=old)]), FakeIG()
        s, _ = self.run_q(cfg(dry=False), n, ig)
        self.assertEqual(s["expired"], ["#01"])
        self.assertEqual(ig.published, [])
        self.assertEqual(n.last_status("a"), "期限切れ")

    def test_publish_failure_marks_error_and_cleans_up(self):
        n, ig = FakeNotion([page("a", "#01"), page("b", "#02")]), FakeIG(fail=True)
        s, host = self.run_q(cfg(dry=False, limit=5), n, ig)
        self.assertEqual(len(s["errors"]), 2)                       # 1件目の失敗で止まらない（公開前の失敗＝エラー）
        self.assertEqual(host.cleared, 2)
        self.assertEqual(n.last_status("a"), "エラー")

    def test_quota_guard(self):
        n, ig = FakeNotion([page("a", "#01")]), FakeIG(quota=60)
        s, _ = self.run_q(cfg(dry=False), n, ig)
        self.assertEqual(ig.published, [])
        self.assertEqual(n.last_status("a"), "エラー")


class SafetyTest(unittest.TestCase):
    def run_q(self, c, notion, ig=None, qa=GOOD_QA):
        host = FakeHost()
        with tempfile.TemporaryDirectory() as d:
            s = post_queue.process(c, notion, ig, Path(d), render=fake_render(qa), host=host)
        return s, host

    def test_publish_failure_is_needs_check_not_error(self):
        n, ig = FakeNotion([page("a", "#01")]), FakeIG(publish_fail=True)
        s, host = self.run_q(cfg(dry=False), n, ig)
        self.assertEqual(ig.publish_calls, 1)                       # 公開は1回だけ
        self.assertEqual(n.last_status("a"), "要確認")
        self.assertEqual(len(s["needs_check"]), 1)
        self.assertEqual(host.cleared, 1)

    def test_after_publish_failures_do_not_mark_error(self):
        n, ig = FakeNotion([page("a", "#01")]), FakeIG(permalink_fail=True)
        s, _ = self.run_q(cfg(dry=False), n, ig)
        self.assertEqual(n.last_status("a"), "投稿済み")
        self.assertEqual(s["posted"][0]["media_id"], "17900000000000001")
        self.assertEqual(n.created[0][1]["メディアID"]["rich_text"][0]["text"]["content"], "17900000000000001")

    def test_queue_update_failure_after_publish_is_reported(self):
        n, ig = FakeNotion([page("a", "#01")], fail_status="投稿済み"), FakeIG()
        s, _ = self.run_q(cfg(dry=False), n, ig)
        self.assertEqual(len(ig.published), 1)
        self.assertEqual(len(s["needs_check"]), 1)
        self.assertNotIn("エラー", [p.get("ステータス", {}).get("status", {}).get("name") for _, p in n.updates])

    def test_missing_status_option_does_not_abort_run(self):
        n, ig = FakeNotion([page("a", "#01"), page("b", "#02")], fail_status="投稿中"), FakeIG()
        s, _ = self.run_q(cfg(dry=False, limit=5), n, ig)
        self.assertEqual(len(s["errors"]), 2)
        self.assertEqual(ig.published, [])

    def test_dry_run_keeps_status_for_problems(self):
        old = (now_jst() - timedelta(hours=60)).isoformat()
        n = FakeNotion([page("a", "#01", due=old), page("b", "#02")])
        bad = {"passed_format_checks": False, "duration_seconds": 20, "warnings": ["はみ出し"]}
        s, _ = self.run_q(cfg(dry=True), n, None, qa=bad)
        self.assertIsNone(n.last_status("a"))
        self.assertIsNone(n.last_status("b"))
        self.assertEqual(s["expired"], [])

    def test_dry_run_parse_is_strict(self):
        from autopost.common import _dry_run
        for v in (None, "", "true", "ture", "on ", "yes please", "False!"):
            self.assertTrue(_dry_run(v), v)
        for v in ("false", "FALSE", " 0", "no", "off"):
            self.assertFalse(_dry_run(v), v)

    def test_hashtags_in_body_counted_but_excel_errors_ignored(self):
        self.assertEqual(post_queue.count_hashtags("省略すると #N/A になる #Excel #経理"), 2)
        spec = dict(SPEC, caption=SPEC["caption"] + "\n#a #b #c")
        problems, _ = post_queue.check_post(spec, GOOD_QA)
        self.assertTrue(any("投稿文全体" in p for p in problems))

    def test_wait_public(self):
        from autopost import hosting
        seq = iter([404, 200, 200])

        class R:
            def __init__(self, st): self.status = st
            def __enter__(self): return self
            def __exit__(self, *a): return False
        hosting.wait_public(["https://x/u1", "https://x/u2"], opener=lambda req, timeout: R(next(seq)), sleep=lambda s: None)
        with self.assertRaises(RuntimeError):
            hosting.wait_public(["https://x/u1"], timeout=20, opener=lambda req, timeout: R(404), sleep=lambda s: None)


class InstagramClientTest(unittest.TestCase):
    def test_publish_flow_and_token_not_in_url_errors(self):
        calls = []
        statuses = iter(["IN_PROGRESS", "FINISHED"])

        def http(method, url, params=None, form=None, retries=2, **kw):
            calls.append((method, url, dict(params or {}), dict(form or {}), retries))
            if url.endswith("/media"):
                return {"id": "C1"}
            if url.endswith("/C1"):
                return {"status_code": next(statuses)}
            if url.endswith("/media_publish"):
                return {"id": "M1"}
            raise AssertionError(url)

        ig = Instagram("SECRET", "U1", http=http, sleep=lambda s: None)
        c = ig.prepare_reel("https://x/v.mp4", "本文", "https://x/c.jpg")
        self.assertFalse(any(x[1].endswith("/media_publish") for x in calls))   # 準備では公開しない
        self.assertEqual(ig.publish(c), "M1")
        self.assertEqual(calls[0][3]["media_type"], "REELS")
        self.assertEqual(calls[0][3]["cover_url"], "https://x/c.jpg")
        for m, url, _, _, retries in calls:
            self.assertNotIn("SECRET", url)                          # トークンはURL本体に入れない
            if m == "POST":
                self.assertEqual(retries, 0)                         # 書き込みは再試行しない
        e = HttpError(400, "bad", "https://graph.instagram.com/v26.0/me?access_token=SECRET")
        self.assertNotIn("SECRET", str(e))

    def test_processing_error_raises(self):
        def http(method, url, **kw):
            return {"id": "C1"} if url.endswith("/media") else {"status_code": "ERROR", "status": "bad video"}
        with self.assertRaises(RuntimeError):
            Instagram("T", "U", http=http, sleep=lambda s: None).prepare_reel("u", "c")

    def test_resolve_user_id_from_page(self):
        seen = []
        def http(m, u, params=None, **kw):
            seen.append(u)
            return {"id": "PAGE1", "instagram_business_account": {"id": "17841400000000000"}}
        ig = Instagram("T", "me", http=http)
        self.assertEqual(ig.resolve_user_id(), "17841400000000000")
        self.assertTrue(seen[0].startswith("https://graph.facebook.com/"))

    def test_resolve_user_id_without_instagram_link(self):
        ig = Instagram("T", "me", http=lambda m, u, **kw: {"id": "PAGE1"})
        with self.assertRaises(RuntimeError):
            ig.resolve_user_id()

    def test_me_and_account_use_ig_id(self):
        def http(m, u, params=None, **kw):
            assert u.endswith("/1784"), u
            return {"id": "1784", "username": "aiaiai4772", "followers_count": 10, "media_count": 6}
        ig = Instagram("T", "1784", http=http)
        self.assertEqual(ig.me(), {"user_id": "1784", "username": "aiaiai4772"})
        self.assertEqual(ig.account()["followers_count"], 10)

    def test_insights_fallback_per_metric(self):
        def http(method, url, params=None, **kw):
            m = params["metric"]
            if "," in m:
                raise HttpError(400, "invalid metric", url)
            if m == "ig_reels_avg_watch_time":
                return {"data": [{"name": m, "values": [{"value": 4200}]}]}
            if m == "views":
                return {"data": [{"name": m, "values": [{"value": 1234}]}]}
            raise HttpError(400, "unsupported", url)
        m = Instagram("T", "U", http=http).insights("M1")
        self.assertEqual(m, {"views": 1234, "ig_reels_avg_watch_time": 4200})
        props = insights.to_props(m)
        self.assertEqual(props["再生数"]["number"], 1234)
        self.assertEqual(props["平均視聴時間_秒"]["number"], 4.2)


class AccountRecordTest(unittest.TestCase):
    def test_account_row_created_then_updated(self):
        class N:
            def __init__(self): self.rows, self.created, self.updated = [], [], []
            def query(self, db, f=None, sorts=None): return self.rows
            def create(self, db, props): self.created.append((db, props)); self.rows = [{"id": "r1"}]
            def update(self, pid, props): self.updated.append((pid, props))

        class IG:
            def account(self): return {"username": "aiaiai4772", "followers_count": 120, "media_count": 9}
        n, c = N(), cfg()
        insights.record_account(c, n, IG())
        insights.record_account(c, n, IG())
        self.assertEqual(n.created[0][0], c.account_db)
        self.assertEqual(n.created[0][1]["フォロワー数"]["number"], 120)
        self.assertEqual(n.created[0][1]["アカウント"]["select"]["name"], "@aiaiai4772")
        self.assertEqual(len(n.created), 1)
        self.assertEqual(n.updated[0][0], "r1")

    def test_account_failure_does_not_raise(self):
        class IG:
            def account(self): raise HttpError(400, "x", "https://graph.instagram.com/me")
        self.assertIsNone(insights.record_account(cfg(), None, IG()))


class CaptionTest(unittest.TestCase):
    def test_caption_matches_post_info(self):
        sys.path.insert(0, str(ROOT / "render"))
        import render_reel
        cap = render_reel.build_caption(SPEC)
        self.assertLessEqual(len(cap), 2200)
        self.assertLessEqual(cap.count("#"), 5 + cap.count("＃") + 10)
        with tempfile.TemporaryDirectory() as d:
            md = render_reel.write_post_info(SPEC, Path(d)).read_text(encoding="utf-8")
        self.assertIn(cap, md)


class TokenCheckTest(unittest.TestCase):
    def run_check(self, info, me_ok=True):
        from autopost import token_check
        import os
        class IG:
            def __init__(self, *a, **k): pass
            def me(self):
                if not me_ok:
                    raise HttpError(400, "bad", "https://graph.facebook.com/v26.0/me")
                return {"user_id": "1784", "username": "aiaiai4772"}
        old = (token_check.Instagram, token_check.debug_token, os.environ.get("IG_ACCESS_TOKEN"))
        token_check.Instagram, token_check.debug_token = IG, lambda t, v: info
        os.environ["IG_ACCESS_TOKEN"] = "T"
        try:
            return token_check.main()
        finally:
            token_check.Instagram, token_check.debug_token = old[0], old[1]
            if old[2] is None:
                os.environ.pop("IG_ACCESS_TOKEN", None)

    def test_page_token_without_expiry_passes(self):
        self.assertEqual(self.run_check({"type": "PAGE", "expires_at": 0}), 0)

    def test_user_token_fails(self):
        self.assertEqual(self.run_check({"type": "USER", "expires_at": 0}), 1)

    def test_expiring_token_fails(self):
        self.assertEqual(self.run_check({"type": "PAGE", "expires_at": 4102444800}), 1)

    def test_broken_token_fails(self):
        self.assertEqual(self.run_check({}, me_ok=False), 1)


class MultiAccountTest(unittest.TestCase):
    def run_q(self, c, notion, ig):
        host = FakeHost()
        with tempfile.TemporaryDirectory() as d:
            s = post_queue.process(c, notion, ig, Path(d), render=fake_render(), host=host)
        return s

    def test_kanji_row_uses_kanji_account(self):
        main, kanji = FakeIG(), FakeIG()
        n = FakeNotion([page("a", "#01"), page("k", "漢字#01", account="漢字")])
        s = self.run_q(cfg(dry=False, limit=1), n, {"main": main, "kanji": kanji})
        self.assertEqual(len(main.published), 1)
        self.assertEqual(len(kanji.published), 1)                 # 上限はアカウントごと
        self.assertEqual(len(s["posted"]), 2)
        labels = [props["アカウント"]["select"]["name"] for _, props in n.created]
        self.assertEqual(labels, ["事務", "漢字"])

    def test_kanji_row_without_secrets_waits_quietly(self):
        main = FakeIG()
        old = (now_jst() - timedelta(hours=60)).isoformat()
        n = FakeNotion([page("k", "漢字#01", account="漢字"), page("k2", "漢字#02", account="漢字", due=old)])
        s = self.run_q(cfg(dry=False), n, {"main": main})
        self.assertEqual(main.published, [])
        self.assertEqual(n.updates, [])                            # ステータスも変えない（エラー通知を出さない）
        self.assertEqual(s["skipped"], 2)
        self.assertEqual(s["errors"], [])

    def test_account_key(self):
        from autopost.common import account_key
        self.assertEqual(account_key({}), "main")
        self.assertEqual(account_key(page("a", "x")), "main")
        self.assertEqual(account_key(page("a", "x", account="漢字")), "kanji")


if __name__ == "__main__":
    unittest.main()
