name: fx-hourly

on:
  schedule:
    # cron은 UTC 기준. 아래는 평일 KST 09~18시 매시 정각 (UTC 00~09).
    # 24시간 전부 받으려면:  - cron: "0 * * * *"
    # 30분마다 받으려면:     - cron: "0,30 0-9 * * 1-5"
    - cron: "0 0-9 * * 1-5"
  workflow_dispatch:
    inputs:
      mode:
        description: "실행 모드"
        type: choice
        default: send
        options: [send, check, dry-run]

permissions:
  contents: read

# 스케줄이 겹쳐 밀릴 때 중복 발송을 막는다
concurrency:
  group: fx-hourly
  cancel-in-progress: false

jobs:
  notify:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - run: pip install --quiet requests

      - name: Send FX rates to Slack
        env:
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
          MIN_CHANGE_PCT: "0"      # 예: "0.15" 로 두면 0.15% 미만 변동일 때 발송 생략
          SKIP_WHEN_CLOSED: "1"    # FX 장 마감(데이터 4시간 이상 정체) 시 발송 생략
          LOG_LEVEL: INFO          # 문제 추적 시 DEBUG
        run: |
          MODE="${{ github.event.inputs.mode || 'send' }}"
          case "$MODE" in
            check)   python fx_exchage.py --check --debug ;;
            dry-run) python fx_exchage.py --dry-run --debug ;;
            *)       python fx_exchage.py ;;
          esac

      - name: Upload log
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: fx-log-${{ github.run_number }}
          path: logs/
          retention-days: 7
          if-no-files-found: warn
