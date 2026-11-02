# reel-autopost（AI運用会社 インスタ部）

Notionの「リール投稿キュー」に予約された台本JSONを、予定時刻に動画化してInstagramの公式APIで投稿し、数字をNotionへ書き戻すGitHub Actionsのキットです。

- はじめての方：**[SETUP_GUIDE.md](SETUP_GUIDE.md)** の手順1〜7だけ実施してください。
- 台本の書き方：[render/SCRIPT_FORMAT.md](render/SCRIPT_FORMAT.md)

## 中身

| パス | 役割 |
|---|---|
| `.github/workflows/autopost.yml` | 30分ごと：キューの「投稿予約」を動画化して投稿 |
| `.github/workflows/insights.yml` | 毎朝：リールの数字をリール実績DBへ |
| `.github/workflows/token.yml` | 毎週：Instagramトークンが使えるか・期限なしのままかを確認 |
| `autopost/post_queue.py` | 投稿の本体（安全装置つき） |
| `autopost/instagram.py` | Instagram API（Facebookログイン版・graph.facebook.com）の呼び出し |
| `autopost/notion.py` | Notion API の呼び出し |
| `autopost/hosting.py` | 投稿中だけ動画を `media` ブランチに置く一時置き場 |
| `render/render_reel.py` | 台本JSON → 1080×1920の動画・表紙・投稿文 |
| `tests/test_offline.py` | ネットにつながずに動く確認テスト（`python -m unittest discover -s tests`） |

## 設定

| 種類 | 名前 | 内容 |
|---|---|---|
| Secret | `NOTION_TOKEN` | Notion内部インテグレーションのシークレット（必須） |
| Secret | `IG_ACCESS_TOKEN` | 期限なしのページアクセストークン（本番で必須） |
| Secret | `IG_USER_ID` | InstagramのID（数字）。未設定ならページにつながったアカウント |
| Variable | `DRY_RUN` | `true`（既定）＝投稿しない／`false`＝本番 |
| Variable | `MAX_POSTS_PER_DAY` | 1日の投稿上限（既定1） |

API利用料：Instagram API・Notion API・GitHub Actions（公開リポジトリ）はいずれも無料枠で動く想定です。再試行は、読み取りの通信の一時エラー（5xx・429）だけ最大2回。投稿・公開などの書き込みは再試行しません。
