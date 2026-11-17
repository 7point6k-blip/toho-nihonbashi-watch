name: TOHOシネマズ日本橋 求人チェック

# GitHubの定期実行(schedule)は混雑時に何時間も間引かれるため、
# 1回の実行の中で10分おきにチェックを繰り返し、約5時間40分続ける方式にしている。
# 毎時起動をかけておけば、前の実行が終わり次第すぐ次が引き継ぐ。
on:
  schedule:
    - cron: "3 * * * *"
  push:
    branches: [main]
    paths: ["watcher.py", ".github/workflows/watch.yml"]
  workflow_dispatch:
    inputs:
      test_notify:
        description: "テスト通知を送る"
        type: boolean
        default: false

permissions:
  contents: write

concurrency:
  group: toho-watch          # 同時に2つ走って状態が壊れるのを防ぐ
  cancel-in-progress: false

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 355
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install
        run: |
          pip install --quiet playwright==1.48.0
          # 標準搭載のChromeを使う。無い場合だけChromiumを入れる
          if ! command -v google-chrome >/dev/null; then python -m playwright install --with-deps chromium; fi

      - name: Check loop (10分おき・約5時間40分)
        env:
          NTFY_TOPIC: ${{ secrets.NTFY_TOPIC || vars.NTFY_TOPIC }}
          TEST_FIRST: ${{ inputs.test_notify && '1' || '0' }}
        run: |
          git config user.name "toho-watch-bot"
          git config user.email "toho-watch-bot@users.noreply.github.com"
          end=$(( $(date +%s) + 340*60 ))
          first=1
          while :; do
            start=$(date +%s)
            if [ "$first" = 1 ]; then export FORCE_TEST_NOTIFY="$TEST_FIRST"; else export FORCE_TEST_NOTIFY=0; fi
            first=0
            timeout 8m python watcher.py || echo "watcher失敗（記録は保存して続行）"
            git add state.json
            if ! git diff --cached --quiet; then
              git commit -m "state update" -q
              for i in 1 2 3; do git pull --rebase -q && git push -q && break; sleep 5; done
            fi
            next=$(( start + 600 ))
            [ $next -ge $end ] && break
            now=$(date +%s); [ $next -gt $now ] && sleep $(( next - now ))
          done
