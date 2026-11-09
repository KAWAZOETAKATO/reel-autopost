#!/usr/bin/env bash
# VOICEVOX（無料の音声合成）を GitHub Actions に用意する。
# 失敗しても動画づくりは標準の声（Open JTalk）で続くので、このステップは continue-on-error で呼ぶ。
# 利用規約：商用・非商用とも利用可。作った音声を使うときは「VOICEVOX:声の名前」のクレジット表記が必要（投稿文に自動で入る）。
set -euo pipefail
VER="0.16.2"
D="$HOME/voicevox"
mkdir -p "$D/models" "$D/onnxruntime"
pip install -q "https://github.com/VOICEVOX/voicevox_core/releases/download/${VER}/voicevox_core-${VER}-cp310-abi3-manylinux_2_34_x86_64.whl"
if [ ! -f "$D/.ready" ]; then
  url=$(gh api repos/VOICEVOX/onnxruntime-builder/releases/latest \
        --jq '.assets[] | select(.name | test("^voicevox_onnxruntime-linux-x64-[0-9.]+\\.tgz$")) | .browser_download_url' | head -1)
  echo "ONNX Runtime: $url"
  curl -sSfL "$url" | tar xz -C "$D/onnxruntime"
  # 0.vvm（四国めたん・ずんだもん・春日部つむぎ）／4.vvm（玄野武宏）／6.vvm（No.7）
  # 声データは最新版だとこのVOICEVOX COREで読めない形式のことがあるので、0.16系の公開分を使う
  gh release list -R VOICEVOX/voicevox_vvm -L 30 || true
  TAG=$(gh api "repos/VOICEVOX/voicevox_vvm/releases?per_page=100" --jq '.[].tag_name' | grep -E '^0\.16\.' | sort -V | tail -1)
  echo "声データ: voicevox_vvm $TAG"
  for n in 0 4 6; do
    gh release download "$TAG" -R VOICEVOX/voicevox_vvm --pattern "${n}.vvm" -D "$D/models" --clobber
  done
  touch "$D/.ready"
fi
ls -la "$D/models"
echo "VOICEVOX_DIR=$D" >> "$GITHUB_ENV"
