"""動画の一時置き場。

Instagram API は「公開URLにある動画」を取りに来る仕組みなので、投稿の間だけ
このリポジトリの media ブランチに動画を置き、raw.githubusercontent.com のURLを渡す。
公開が終わったら media ブランチを空に戻す（動画はリールとして公開済みのものだけが一時的に置かれる）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

BRANCH = "media"


def _git(cwd: str, *args: str) -> None:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        # 出力にトークン入りURLが混ざらないよう、エラー文はgitの標準エラーの末尾だけ・URLは伏せる
        err = r.stderr.replace(os.environ.get("GITHUB_TOKEN", "\0"), "***")
        raise RuntimeError(f"git {args[0]} に失敗: {err[-400:]}")


def _push_tree(repo: str, files: dict[str, Path], message: str) -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token or not repo:
        raise RuntimeError("GITHUB_TOKEN / GITHUB_REPOSITORY がありません（GitHub Actions上で実行してください）")
    tmp = tempfile.mkdtemp()
    try:
        _git(tmp, "init", "-q", "-b", BRANCH)
        _git(tmp, "config", "user.name", "reel-autopost")
        _git(tmp, "config", "user.email", "reel-autopost@users.noreply.github.com")
        Path(tmp, "README.md").write_text("自動投稿の一時置き場です。投稿が終わると自動で空になります。\n", encoding="utf-8")
        for name, src in files.items():
            shutil.copy(src, Path(tmp, name))
        _git(tmp, "add", "-A")
        _git(tmp, "commit", "-q", "-m", message)
        _git(tmp, "push", "-q", "--force", f"https://x-access-token:{token}@github.com/{repo}.git", f"HEAD:{BRANCH}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def upload(repo: str, files: dict[str, Path]) -> dict[str, str]:
    """files = {公開ファイル名: ローカルパス}。公開URLの辞書を返す。"""
    _push_tree(repo, files, "一時アップロード")
    return {name: f"https://raw.githubusercontent.com/{repo}/{BRANCH}/{name}" for name in files}


def clear(repo: str) -> None:
    _push_tree(repo, {}, "一時ファイルを削除")


def wait_public(urls: list[str], timeout: int = 300, every: int = 10, opener=urllib.request.urlopen, sleep=time.sleep) -> None:
    """アップした直後は公開URLがまだ404のことがあるので、全部が取得できるようになるまで待つ。"""
    waited = 0
    pending = list(urls)
    while pending:
        still = []
        for u in pending:
            try:
                with opener(urllib.request.Request(u, method="HEAD"), timeout=30) as r:
                    if r.status != 200:
                        still.append(u)
            except Exception:
                still.append(u)
        pending = still
        if not pending:
            return
        if waited >= timeout:
            raise RuntimeError(f"一時置き場のファイルが{timeout}秒たっても公開されませんでした：{', '.join(p.rsplit('/', 1)[-1] for p in pending)}")
        sleep(every)
        waited += every
