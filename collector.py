"""올림픽공원 실시간 주차현황 수집기

사용법
  python collector.py --once --no-db   # API만 한 번 확인 (DB 저장 안 함)
  python collector.py --once           # 한 번 수집해서 DB까지 저장 (실패 시 종료코드 1)
  python collector.py                  # 5분마다 계속 수집
"""
import argparse
import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

URL = "https://www.ksponco.or.kr/online/main/parkingorsApi.do"
API_KEY = (os.getenv("PARKING_API_KEY") or "").strip()
if not API_KEY:
    raise SystemExit(".env에 PARKING_API_KEY가 필요해요.")

TABLE = "parking_snapshots"
LOG_CSV = BASE / "parking_log.csv"
MAX_PENDING = 5000

pending: list[dict] = []  # DB 저장에 실패해서 재시도 대기 중인 행
_db = None


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fetch_total():
    """주차 합계를 가져온다. 3번 시도해도 안 되면 None."""
    for attempt in range(1, 4):
        started = time.monotonic()
        try:
            r = httpx.get(
                URL,
                params={"apiKey": API_KEY},
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
            latency = int((time.monotonic() - started) * 1000)
            if r.status_code in (401, 403):
                # 인증 문제는 재시도해도 소용없으므로 바로 중단
                print(f"[{stamp()}] 인증 오류(HTTP {r.status_code}): PARKING_API_KEY를 확인하세요.")
                return None
            if r.status_code != 200:
                print(f"[{stamp()}] 시도 {attempt}: HTTP {r.status_code}")
            else:
                data = r.json()
                total = int(data[0]["parking_sum"])
                if total < 0:
                    raise ValueError("negative")
                return total, data, latency
        except httpx.HTTPError as e:
            # 오류 메시지에는 주소(키 포함)가 있을 수 있어서 종류만 출력
            print(f"[{stamp()}] 시도 {attempt}: 연결 오류 ({type(e).__name__})")
        except (ValueError, KeyError, IndexError, TypeError):
            print(f"[{stamp()}] 시도 {attempt}: 응답 형식이 예상과 달라요")
        if attempt < 3:
            time.sleep(5 * attempt)
    return None


def append_csv(ts: datetime, total: int, latency: int) -> None:
    """로컬 백업. DB가 안 돼도 기록이 남는다."""
    is_new = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["collected_at_utc", "parking_sum", "latency_ms"])
        w.writerow([ts.isoformat(), total, latency])


def get_db():
    global _db
    if _db is None:
        from db import supabase  # --no-db 모드에서는 불러오지 않는다

        _db = supabase
    return _db


def flush_pending() -> bool:
    """대기 중인 행을 저장한다. 성공(또는 저장할 것 없음)이면 True."""
    if not pending:
        return True
    try:
        get_db().table(TABLE).insert(pending).execute()
        print(f"[{stamp()}] DB 저장 {len(pending)}건 완료")
        pending.clear()
        return True
    except Exception as e:
        print(
            f"[{stamp()}] DB 저장 실패 (대기 {len(pending)}건, 다음 주기에 재시도): "
            f"{type(e).__name__}: {str(e)[:150]}"
        )
        if len(pending) > MAX_PENDING:
            del pending[: len(pending) - MAX_PENDING]
        return False


def collect_once(use_db: bool) -> bool:
    """한 번 수집한다. 성공하면 True, 실패하면 False."""
    result = fetch_total()
    if result is None:
        print(f"[{stamp()}] 수집 실패 (이번 주기는 건너뜀)")
        if use_db:
            flush_pending()
        return False

    total, raw, latency = result
    ts = datetime.now(timezone.utc)
    append_csv(ts, total, latency)
    print(f"[{stamp()}] 주차 합계: {total} ({latency}ms)")

    if use_db:
        pending.append(
            {
                "collected_at": ts.isoformat(),
                "zone_name": "ALL",
                "occupied": total,
                "raw": {"api": raw, "latency_ms": latency},
            }
        )
        return flush_pending()
    return True


def sleep_to_next_slot(interval: int) -> None:
    """interval 초 단위 정각(예: 5분이면 :00, :05, :10…)까지 기다린다."""
    time.sleep(interval - (time.time() % interval))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="한 번만 수집하고 종료")
    p.add_argument("--no-db", action="store_true", help="DB에 저장하지 않음")
    p.add_argument("--interval", type=int, default=300, help="수집 주기(초), 최소 60")
    args = p.parse_args()

    use_db = not args.no_db
    interval = max(args.interval, 60)

    if args.once:
        # 실패하면 종료코드 1 → GitHub Actions가 빨간색으로 표시된다
        ok = collect_once(use_db)
        raise SystemExit(0 if ok else 1)

    print(f"수집 시작: {interval}초 주기, DB 저장={'예' if use_db else '아니오'} (중지: Ctrl+C)")
    try:
        while True:
            collect_once(use_db)
            sleep_to_next_slot(interval)
    except KeyboardInterrupt:
        print("\n수집을 중지했어요.")


if __name__ == "__main__":
    main()
