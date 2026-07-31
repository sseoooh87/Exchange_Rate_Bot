#!/usr/bin/env python3
"""
슬랙으로 원/달러 · 원/엔 환율을 주기적으로 발송한다.

사용법
  py fx_slack.py              # 발송
  py fx_slack.py --check      # 설정 + 데이터 점검만
  py fx_slack.py --dry-run    # payload 출력 (웹훅 없어도 됨)
  py fx_slack.py --debug      # 상세 로그
  py fx_slack.py --tail       # 최근 로그

환경변수 (.env 자동 인식)
  SLACK_WEBHOOK_URL   (필수)
  MIN_CHANGE_PCT      이 값 미만 변동이면 발송 생략 (기본 0 = 항상 발송)
  SKIP_WHEN_CLOSED    1이면 FX 장 마감 시 발송 생략 (기본 1)
  LOG_DIR / LOG_LEVEL

종료 코드
  0 정상(발송 또는 의도적 생략) / 1 설정 오류 / 2 데이터 오류 / 3 슬랙 발송 실패

데이터 출처: Yahoo Finance (v8 chart), 폴백 open.er-api.com
"""

import argparse
import datetime as dt
import json
import logging
import logging.handlers
import os
import pathlib
import sys
import time
import traceback

try:
    import requests
except ImportError:
    sys.exit("requests 모듈이 없습니다.  pip install requests")

KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(KST)
TIMEOUT = 15
RETRIES = 3
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

BARS = "▁▂▃▄▅▆▇█"
SPARK_POINTS = 24          # 스파크라인에 쓸 최근 데이터 개수

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

log = logging.getLogger("fx_slack")


# ============================================================ 로깅 / 설정

class KstFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return dt.datetime.fromtimestamp(record.created, KST).strftime(
            datefmt or "%Y-%m-%d %H:%M:%S")


def setup_logging(debug: bool) -> pathlib.Path:
    log_dir = pathlib.Path(os.getenv("LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "fx_slack.log"
    level = logging.DEBUG if debug else getattr(
        logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False
    fmt = KstFormatter("%(asctime)s KST %(levelname)-8s %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return path


def load_dotenv(path=".env") -> bool:
    p = pathlib.Path(path)
    if not p.exists():
        return False
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if " #" in v:
            v = v.split(" #")[0].strip()
        if not os.environ.get(k):
            os.environ[k] = v
    return True


def mask(url: str) -> str:
    if not url:
        return "(비어 있음)"
    return f"https://hooks.slack.com/services/***/***/***{url[-4:]}  (총 {len(url)}자)"


class ConfigError(Exception):
    pass


class DataError(Exception):
    pass


# ============================================================ 통화 정의

class Pair:
    """
    symbol : Yahoo 티커
    scale  : 표시 배수 (엔은 100엔 기준이 국내 관행이라 100)
    """

    def __init__(self, key, label, symbol, scale=1, digits=2, icon="💵"):
        self.key, self.label, self.symbol = key, label, symbol
        self.scale, self.digits, self.icon = scale, digits, icon
        self.price = self.prev = None
        self.series: list = []
        self.stamp = None
        self.source = ""

    @property
    def shown(self):
        return None if self.price is None else self.price * self.scale

    @property
    def shown_prev(self):
        return None if self.prev is None else self.prev * self.scale

    @property
    def diff(self):
        if self.price is None or self.prev is None:
            return None
        return (self.price - self.prev) * self.scale

    @property
    def pct(self):
        if self.price is None or not self.prev:
            return None
        return (self.price - self.prev) / self.prev * 100

    def __repr__(self):
        return f"{self.key}({self.symbol})"


def make_pairs():
    return [
        Pair("USD", "USD / KRW", "USDKRW=X", 1, 2, "💵"),
        Pair("JPY", "JPY / KRW", "JPYKRW=X", 100, 2, "💴"),
    ]


# ============================================================ 데이터 수집

def http_json(name, url, params=None, retries=RETRIES):
    last = None
    for attempt in range(1, retries + 1):
        t0 = time.perf_counter()
        try:
            r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
            ms = (time.perf_counter() - t0) * 1000
            log.debug("%s | HTTP %s · %.0fms · %dbytes",
                      name, r.status_code, ms, len(r.content))
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            log.warning("%s | 시도 %d/%d 실패 %s: %s",
                        name, attempt, retries, type(e).__name__, e)
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise DataError(f"{name} 조회 실패: {type(last).__name__}: {last}")


def fetch_yahoo(pair: Pair) -> None:
    """v8 chart 엔드포인트. 키 불필요, User-Agent만 있으면 된다."""
    d = http_json(f"yahoo/{pair.key}",
                  f"https://query1.finance.yahoo.com/v8/finance/chart/{pair.symbol}",
                  {"range": "5d", "interval": "1h"})
    result = (d.get("chart", {}).get("result") or [None])[0]
    if not result:
        err = d.get("chart", {}).get("error")
        raise DataError(f"{pair.key} 응답에 result 없음: {err}")

    meta = result.get("meta", {})
    closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    stamps = result.get("timestamp") or []

    clean = [(t, c) for t, c in zip(stamps, closes) if c is not None]
    if not clean:
        raise DataError(f"{pair.key} 시계열이 비어 있음")

    pair.series = [c for _, c in clean][-SPARK_POINTS:]
    pair.stamp = dt.datetime.fromtimestamp(clean[-1][0], KST)
    pair.price = meta.get("regularMarketPrice") or clean[-1][1]
    pair.prev = (meta.get("chartPreviousClose") or meta.get("previousClose")
                 or (clean[0][1] if clean else None))
    pair.source = "Yahoo Finance"

    log.info("%s | %.4f (전일 %.4f) · 시계열 %d개 · 최신 %s",
             pair.key, pair.price, pair.prev or 0, len(pair.series),
             pair.stamp.strftime("%m/%d %H:%M"))


def fetch_fallback(pairs) -> None:
    """Yahoo 실패 시 최소한의 현재가라도 채운다 (일 1회 갱신, 시계열 없음)."""
    d = http_json("fallback", "https://open.er-api.com/v6/latest/USD")
    rates = d.get("rates", {})
    krw, jpy = rates.get("KRW"), rates.get("JPY")
    if not krw:
        raise DataError("폴백 응답에 KRW 없음")
    for p in pairs:
        if p.price is not None:
            continue
        if p.key == "USD":
            p.price = krw
        elif p.key == "JPY" and jpy:
            p.price = krw / jpy
        p.prev = p.prev or p.price
        p.source = "open.er-api.com (일 1회 갱신)"
        log.warning("%s | 폴백 데이터 사용: %.4f", p.key, p.price or 0)


def collect(pairs) -> None:
    failed = []
    for p in pairs:
        try:
            fetch_yahoo(p)
        except Exception as e:
            log.warning("%s | Yahoo 실패: %s", p.key, e)
            failed.append(p)
    if failed:
        try:
            fetch_fallback(pairs)
        except Exception as e:
            log.error("폴백도 실패: %s", e)
    alive = [p for p in pairs if p.price is not None]
    if not alive:
        raise DataError("모든 소스에서 환율을 가져오지 못했습니다.")
    log.info("수집 | %d/%d 통화 확보", len(alive), len(pairs))


# ============================================================ 표현

def spark(values) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return BARS[3] * len(values)
    return "".join(BARS[min(len(BARS) - 1, int((v - lo) / (hi - lo) * len(BARS)))]
                   for v in values)


def arrow(diff) -> tuple:
    """국내 관행: 상승 빨강(▲), 하락 파랑(▼)."""
    if diff is None:
        return "·", "#94a3b8"
    if diff > 0:
        return "▲", "#ef4444"
    if diff < 0:
        return "▼", "#3b82f6"
    return "―", "#94a3b8"


def market_open(pairs) -> bool:
    """마지막 데이터가 4시간 이상 묵었으면 장 마감으로 간주."""
    stamps = [p.stamp for p in pairs if p.stamp]
    if not stamps:
        return True
    age = (NOW - max(stamps)).total_seconds() / 3600
    log.info("시장 | 최신 데이터 %.1f시간 전", age)
    return age < 4


def build_payload(pairs) -> dict:
    lines, blocks = [], []
    usd = next((p for p in pairs if p.key == "USD"), None)
    _, color = arrow(usd.diff if usd else None)

    blocks.append({"type": "header", "text": {"type": "plain_text",
                   "text": f"환율  {NOW:%m월 %d일 %H:%M}", "emoji": True}})

    for p in pairs:
        if p.price is None:
            continue
        mark, _ = arrow(p.diff)
        unit = " _(100엔 기준)_" if p.scale == 100 else ""
        head = (f"{p.icon}  *{p.label}*{unit}\n"
                f"*`{p.shown:,.{p.digits}f}`*  원")
        if p.diff is not None and p.pct is not None:
            head += f"　{mark} {abs(p.diff):,.{p.digits}f}  ({p.pct:+.2f}%)"

        sub = []
        if len(p.series) >= 2:
            scaled = [v * p.scale for v in p.series]
            sub.append(f"`{spark(p.series)}`")
            sub.append(f"저 {min(scaled):,.{p.digits}f} · 고 {max(scaled):,.{p.digits}f}")
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": head + ("\n" + "　".join(sub) if sub else "")}})
        lines.append(f"{p.key} {p.shown:,.{p.digits}f}"
                     + (f" ({p.pct:+.2f}%)" if p.pct is not None else ""))

    src = " · ".join(sorted({p.source for p in pairs if p.source}))
    stamps = [p.stamp for p in pairs if p.stamp]
    latest = max(stamps).strftime("%m/%d %H:%M") if stamps else "-"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"최근 {SPARK_POINTS}시간 추이 · 기준시각 {latest} · {src}"}]})

    payload = {"text": " | ".join(lines) or "환율 정보",
               "attachments": [{"color": color, "blocks": blocks}]}
    size = len(json.dumps(payload, ensure_ascii=False).encode())
    log.info("조립 | 블록 %d개 · %dbytes · %s", len(blocks), size, " | ".join(lines))
    return payload


# ============================================================ 발송

SLACK_HINTS = {
    "no_service": "웹훅이 삭제되었거나 URL이 틀렸습니다. api.slack.com/apps 에서 재발급하세요.",
    "no_team": "워크스페이스에서 앱이 제거되었습니다.",
    "invalid_payload": "블록 구조 오류입니다. --dry-run 출력을 Block Kit Builder에서 확인하세요.",
    "channel_not_found": "웹훅에 연결된 채널이 삭제되었거나 접근 불가합니다.",
}


def send(webhook: str, payload: dict) -> None:
    for attempt in range(1, RETRIES + 1):
        t0 = time.perf_counter()
        try:
            r = requests.post(webhook, json=payload, timeout=TIMEOUT)
        except Exception as e:
            log.warning("발송 | 시도 %d/%d 네트워크 오류 %s: %s",
                        attempt, RETRIES, type(e).__name__, e)
            if attempt == RETRIES:
                raise DataError(f"슬랙 발송 실패 (네트워크): {e}")
            time.sleep(2 ** attempt)
            continue
        body = r.text.strip()
        log.info("발송 | HTTP %s · %.0fms · '%s'",
                 r.status_code, (time.perf_counter() - t0) * 1000, body[:120])
        if r.status_code == 200 and body == "ok":
            return
        if body in SLACK_HINTS:
            log.error("발송 | 원인: %s", SLACK_HINTS[body])
        if 400 <= r.status_code < 500:
            raise DataError(f"슬랙이 요청을 거부했습니다: {r.status_code} '{body}'")
        if attempt == RETRIES:
            raise DataError(f"슬랙 발송 실패: {r.status_code} '{body}'")
        time.sleep(2 ** attempt)


# ============================================================ main

def tail_log(n=50):
    path = pathlib.Path(os.getenv("LOG_DIR", "logs")) / "fx_slack.log"
    if not path.exists():
        print(f"로그 파일이 아직 없습니다: {path.resolve()}")
        return
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    try:
        print(f"--- {path.resolve()}  (마지막 {min(n, len(lines))} / 전체 {len(lines)}줄) ---")
        print("\n".join(lines[-n:]))
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def main() -> int:
    ap = argparse.ArgumentParser(description="슬랙 환율 알림 (USD · JPY)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--tail", action="store_true")
    args = ap.parse_args()

    if args.tail:
        tail_log()
        return 0

    had_env = load_dotenv()
    log_path = setup_logging(args.debug)
    started = time.perf_counter()
    mode = "check" if args.check else "dry-run" if args.dry_run else "send"

    log.info("=" * 66)
    log.info("시작 | %s KST · 모드=%s", NOW.strftime("%Y-%m-%d %H:%M:%S"), mode)
    log.info("시작 | 로그=%s · 폴더=%s · .env %s",
             log_path.resolve(), pathlib.Path.cwd(), "읽음" if had_env else "없음")
    if os.getenv("GITHUB_ACTIONS"):
        log.info("시작 | GitHub Actions (repo=%s run#%s)",
                 os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_RUN_NUMBER"))

    try:
        webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
        min_pct = float(os.getenv("MIN_CHANGE_PCT", "0"))
        skip_closed = os.getenv("SKIP_WHEN_CLOSED", "1") == "1"
        log.info("설정 | 웹훅=%s", mask(webhook))
        log.info("설정 | 최소변동 %.2f%% · 마감시 생략 %s", min_pct, skip_closed)

        if mode == "send" and not webhook:
            raise ConfigError(
                "SLACK_WEBHOOK_URL 이 설정되지 않았습니다.\n"
                "  로컬: 이 폴더에 .env 파일 → SLACK_WEBHOOK_URL=https://hooks.slack.com/...\n"
                "  Actions: Settings → Secrets → SLACK_WEBHOOK_URL 등록")

        pairs = make_pairs()
        collect(pairs)

        if skip_closed and not market_open(pairs):
            log.info("생략 | FX 장 마감 상태 — 발송하지 않습니다.")
            log.info("종료 | 소요 %.2fs · 코드 0", time.perf_counter() - started)
            return 0

        moves = [abs(p.pct) for p in pairs if p.pct is not None]
        if min_pct > 0 and moves and max(moves) < min_pct:
            log.info("생략 | 최대 변동 %.2f%% < 기준 %.2f%%", max(moves), min_pct)
            log.info("종료 | 소요 %.2fs · 코드 0", time.perf_counter() - started)
            return 0

        payload = build_payload(pairs)

        if mode == "check":
            ready = bool(webhook)
            log.info("점검 | 데이터 정상 · 조립 정상 · 웹훅 %s",
                     "설정됨 ✅" if ready else "미설정 ❌")
            log.info("종료 | 소요 %.2fs · 코드 %d",
                     time.perf_counter() - started, 0 if ready else 1)
            return 0 if ready else 1
        if mode == "dry-run":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            log.info("dry-run | 출력 완료 (발송 안 함)")
        else:
            send(webhook, payload)
            log.info("완료 | 발송 성공 ✅")

        log.info("종료 | 소요 %.2fs · 코드 0", time.perf_counter() - started)
        return 0

    except ConfigError as e:
        log.error("설정 오류 ↓\n%s", e)
        log.info("종료 | 소요 %.2fs · 코드 1", time.perf_counter() - started)
        return 1
    except DataError as e:
        code = 3 if "슬랙" in str(e) else 2
        log.error("오류: %s", e)
        log.debug("트레이스백:\n%s", traceback.format_exc())
        log.info("종료 | 소요 %.2fs · 코드 %d", time.perf_counter() - started, code)
        return code
    except KeyboardInterrupt:
        log.warning("사용자가 중단했습니다.")
        return 130
    except Exception:
        log.critical("예상치 못한 오류 ↓\n%s", traceback.format_exc())
        log.info("종료 | 소요 %.2fs · 코드 2", time.perf_counter() - started)
        return 2


if __name__ == "__main__":
    sys.exit(main())
