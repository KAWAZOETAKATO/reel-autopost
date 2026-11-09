"""Instagram API（Facebookログイン版・公式）でリールを投稿し、数字を取得する。

送り先は graph.facebook.com。トークンは「期限なしのページアクセストークン」（手順書の手順4）。
使う権限：instagram_basic / instagram_content_publish / pages_read_engagement / pages_show_list / business_management
（数字の取得には instagram_manage_insights も必要）
"""
from __future__ import annotations

import time

from .common import HttpError, http_json, log


class Instagram:
    def __init__(self, token: str, user_id: str, version: str = "v26.0", http=http_json, sleep=time.sleep):
        self._token = token
        self.user_id = user_id
        self.base = f"https://graph.facebook.com/{version}"
        self._http = http
        self._sleep = sleep

    # トークンは常にPOSTのフォームかクエリで送る（ログには出さない：HttpErrorがクエリを消す）
    def _get(self, path: str, **params) -> dict:
        params["access_token"] = self._token
        return self._http("GET", f"{self.base}/{path}", params=params)

    def _post(self, path: str, **form) -> dict:
        form["access_token"] = self._token
        return self._http("POST", f"{self.base}/{path}", form=form, retries=0)   # 書き込みは再試行しない（二重投稿防止）

    def resolve_user_id(self) -> str:
        """投稿先のInstagram ID を、ページトークンのページにつながったInstagramから決める。

        IG_USER_ID が設定されていても、それがInstagramのIDでない（FacebookページのIDなど）と投稿できないため、
        トークンから分かるIDを優先する。分からないときだけ設定値を使う。"""
        if getattr(self, "_resolved", False):
            return self.user_id
        configured = "" if self.user_id in ("", "me") else str(self.user_id)
        try:
            page = self._get("me", fields="id,name,instagram_business_account")
            ig = (page.get("instagram_business_account") or {}).get("id")
        except HttpError:
            ig = None
            if not configured:
                raise
        if ig:
            if configured and configured != str(ig):
                log("注意：設定されたInstagram ID は、トークンのページにつながったInstagramと違います。"
                    "トークンのページにつながったInstagram に投稿します（Secrets の IG_USER_ID は直さなくても動きます）")
            self.user_id = str(ig)
        elif not configured:
            raise RuntimeError("トークンのページにInstagramがつながっていません。"
                               "ページアクセストークン（手順書の手順4）を使っているか、"
                               "InstagramとFacebookページのつながり（手順1）を確認してください")
        self._resolved = True
        return self.user_id

    def me(self) -> dict:
        """投稿先のInstagramアカウント（user_id・username）。"""
        uid = self.resolve_user_id()
        r = self._get(uid, fields="id,username")
        return {"user_id": r.get("id", uid), "username": r.get("username", "")}

    def account(self) -> dict:
        """アカウント全体の数字（ユーザー名・フォロワー数・投稿数）。"""
        uid = self.resolve_user_id()
        r = self._get(uid, fields="id,username,followers_count,media_count")
        r["user_id"] = r.get("id", uid)
        return r

    def quota_used(self) -> int:
        self.resolve_user_id()
        r = self._get(f"{self.user_id}/content_publishing_limit", fields="quota_usage")
        data = r.get("data") or [{}]
        return int(data[0].get("quota_usage", 0))

    def prepare_reel(self, video_url: str, caption: str, cover_url: str | None = None,
                     wait_seconds: int = 600, poll: int = 30) -> str:
        """動画コンテナを作り、Instagram側の処理完了まで待つ（まだ公開しない）。コンテナIDを返す。"""
        form = {"media_type": "REELS", "video_url": video_url, "caption": caption, "share_to_feed": "true"}
        if cover_url:
            form["cover_url"] = cover_url
        container = self._post(f"{self.user_id}/media", **form)["id"]
        log(f"動画コンテナ作成：{container}（Instagram側の処理待ち）")
        waited = 0
        while True:
            st = self._get(container, fields="status_code,status")
            code = st.get("status_code")
            if code == "FINISHED":
                return container
            if code in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Instagram側の動画処理に失敗：{code} {st.get('status', '')}")
            if waited >= wait_seconds:
                raise RuntimeError(f"Instagram側の動画処理が{wait_seconds}秒で終わりませんでした（最後の状態：{code}）")
            self._sleep(poll)
            waited += poll

    def publish(self, container: str) -> str:
        """公開（1回だけ呼ぶ）。公開したメディアIDを返す。"""
        return self._post(f"{self.user_id}/media_publish", creation_id=container)["id"]

    def permalink(self, media_id: str) -> str:
        return self._get(media_id, fields="permalink").get("permalink", "")

    def insights(self, media_id: str) -> dict:
        """リールの数字。取れない指標は飛ばす（指標名はMeta側で変わることがあるため1つずつ取得）。"""
        metrics = ["views", "reach", "likes", "comments", "shares", "saved", "ig_reels_avg_watch_time"]
        out = {}
        try:
            r = self._get(f"{media_id}/insights", metric=",".join(metrics))
            rows = r.get("data", [])
        except HttpError:
            rows = []
            for m in metrics:
                try:
                    rows += self._get(f"{media_id}/insights", metric=m).get("data", [])
                except HttpError as e:
                    log(f"指標 {m} は取得できませんでした（{e.status}）")
        for row in rows:
            vals = row.get("values") or [{}]
            v = row.get("total_value", {}).get("value", vals[0].get("value"))
            if v is not None:
                out[row["name"]] = v
        return out


def debug_token(token: str, version: str = "v26.0", http=http_json) -> dict:
    """トークンの種類と有効期限を調べる（自分自身で調べる。expires_at=0 は期限なし）。"""
    r = http("GET", f"https://graph.facebook.com/{version}/debug_token",
             params={"input_token": token, "access_token": token})
    return r.get("data", {})
