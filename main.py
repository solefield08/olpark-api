from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from db import supabase

KST = timezone(timedelta(hours=9))
TABLE = "parking_snapshots"

app = FastAPI(title="Olpark Parking API")

# Flutter Web 등 다른 주소의 화면이 읽을 수 있게 허용한다.
# 이 API는 조회(GET)만 있고 공개 가능한 집계 값만 돌려주므로 모든 출처를 허용한다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def to_kst(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KST)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/parking/current")
def current():
    res = (
        supabase.table(TABLE)
        .select("collected_at,occupied")
        .eq("zone_name", "ALL")
        .order("collected_at", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="수집된 데이터가 없어요")
    row = res.data[0]
    t = to_kst(row["collected_at"])
    age = int((datetime.now(KST) - t).total_seconds() // 60)
    return {
        "occupied": row["occupied"],
        "collected_at_kst": t.isoformat(),
        "minutes_ago": age,
        "stale": age > 20,  # 20분 넘게 새 데이터가 없으면 true
    }


@app.get("/parking/today")
def today():
    start = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    res = (
        supabase.table(TABLE)
        .select("collected_at,occupied")
        .eq("zone_name", "ALL")
        .gte("collected_at", start.isoformat())
        .order("collected_at")
        .limit(1000)
        .execute()
    )
    return [
        {"time": to_kst(r["collected_at"]).strftime("%H:%M"), "occupied": r["occupied"]}
        for r in res.data
    ]


@app.get("/parking/hourly")
def hourly(days: int = 14):
    days = max(1, min(days, 60))
    res = supabase.rpc("hourly_avg", {"p_days": days}).execute()
    return res.data


@app.get("/parking/level")
def level(days: int = 14):
    cur = current()
    now = datetime.now(KST)
    weekday = (now.weekday() + 1) % 7  # DB 기준: 일요일=0
    stats = supabase.rpc("hourly_avg", {"p_days": max(1, min(days, 60))}).execute().data
    same = [s for s in stats if s["weekday"] == weekday and s["hour"] == now.hour]

    if not same or int(same[0]["days"]) < 3:
        return {
            "occupied": cur["occupied"],
            "level": None,
            "note": "같은 요일·시간의 과거 데이터가 아직 부족해요",
        }

    baseline = float(same[0]["avg_occupied"])
    ratio = cur["occupied"] / baseline if baseline else None
    # 임시 기준값이에요. 데이터가 쌓이고 값의 의미가 확인되면 조정해야 해요.
    label = "보통"
    if ratio is not None:
        label = "여유" if ratio < 0.85 else ("혼잡" if ratio > 1.15 else "보통")
    return {
        "occupied": cur["occupied"],
        "baseline": baseline,
        "ratio": round(ratio, 2) if ratio else None,
        "level": label,
        "samples": same[0]["samples"],
        "note": "과거 같은 요일·시간 평균 대비 참고용 지표예요",
    }


# ---------------------------------------------------------------
# v0.3 기획서 추가 기능: 신뢰 표시(meta), 오늘·평소 곡선, 추천 시간대, 1~3시간 전망
# ---------------------------------------------------------------
MIN_DAYS = 3  # "평소"로 쓰려면 필요한 서로 다른 과거 날짜 수
OPEN_HOURS = (9, 21)  # 추천 시간대 후보 범위 (임시값. 공원 운영 시간 확인 전)


def _dow(dt: datetime) -> int:
    return (dt.weekday() + 1) % 7  # DB 기준: 일요일=0


def _usual(days: int = 28) -> dict:
    """{(요일, 시간): hourly_avg 행}. 오늘은 제외된 과거 평균이다."""
    rows = supabase.rpc("hourly_avg", {"p_days": max(1, min(days, 60))}).execute().data
    return {(r["weekday"], r["hour"]): r for r in rows}


@app.get("/parking/meta")
def meta():
    """신뢰 표시용: 수집 기간, 건수, 마지막 갱신."""
    first = (
        supabase.table(TABLE)
        .select("collected_at")
        .eq("zone_name", "ALL")
        .order("collected_at")
        .limit(1)
        .execute()
        .data
    )
    if not first:
        raise HTTPException(status_code=404, detail="수집된 데이터가 없어요")
    total = (
        supabase.table(TABLE)
        .select("id", count="exact")
        .eq("zone_name", "ALL")
        .limit(1)
        .execute()
        .count
    )
    cur = current()
    first_kst = to_kst(first[0]["collected_at"])
    past_days = (datetime.now(KST).date() - first_kst.date()).days
    return {
        "first_collected_kst": first_kst.isoformat(),
        "past_days": past_days,  # 수집 시작일부터 어제까지의 달력상 일수 (결측일 포함)
        "total_rows": total,
        "last_collected_kst": cur["collected_at_kst"],
        "minutes_ago": cur["minutes_ago"],
        "min_days": MIN_DAYS,
    }


@app.get("/parking/curve")
def curve():
    """오늘 5분 단위 값 + 같은 요일의 평소 곡선(표본 일수가 충분한 시간대만)."""
    now = datetime.now(KST)
    wd = _dow(now)
    usual = [
        {"hour": h, "avg_occupied": r["avg_occupied"], "days": r["days"]}
        for (w, h), r in sorted(_usual().items())
        if w == wd and int(r["days"]) >= MIN_DAYS
    ]
    return {
        "weekday": wd,
        "today": today(),
        "usual": usual,
        "min_days": MIN_DAYS,
        "note": "평소 곡선은 서로 다른 과거 날짜가 충분한 시간대만 표시해요",
    }


@app.get("/parking/recommend")
def recommend(top: int = 3, weekday: int | None = None):
    """요일별로 값이 낮은 방문 추천 시간대. weekday: 일요일=0, 기본은 오늘 요일."""
    wd = _dow(datetime.now(KST)) if weekday is None else weekday
    top = max(1, min(top, 6))
    cand = [
        (float(r["avg_occupied"]), h)
        for (w, h), r in _usual().items()
        if w == wd and OPEN_HOURS[0] <= h <= OPEN_HOURS[1] and int(r["days"]) >= MIN_DAYS
    ]
    if len(cand) < top:
        return {
            "weekday": wd,
            "hours": [],
            "note": "같은 요일의 과거 데이터가 아직 부족해서 추천할 수 없어요",
        }
    cand.sort()
    return {
        "weekday": wd,
        "hours": [{"hour": h, "avg_occupied": round(v, 1)} for v, h in sorted(cand[:top], key=lambda x: x[1])],
        "range": list(OPEN_HOURS),
        "note": "과거 평균이 낮은 시간대 참고용이에요 (후보 시간 범위는 임시값)",
    }


@app.get("/parking/forecast")
def forecast():
    """1~3시간 뒤 전망 = 평소(t+h) x (현재값 / 평소(t)). 근거가 부족하면 null."""
    cur = current()
    now = datetime.now(KST)
    usual = _usual()
    base = usual.get((_dow(now), now.hour))
    out = []
    for h in (1, 2, 3):
        t = now + timedelta(hours=h)
        tgt = usual.get((_dow(t), t.hour))
        ok = (
            base is not None
            and tgt is not None
            and int(base["days"]) >= MIN_DAYS
            and int(tgt["days"]) >= MIN_DAYS
            and float(base["avg_occupied"]) > 0
        )
        value = (
            round(float(tgt["avg_occupied"]) * cur["occupied"] / float(base["avg_occupied"]))
            if ok
            else None
        )
        out.append({"hours_ahead": h, "target_hour": t.hour, "forecast": value})
    return {
        "occupied": cur["occupied"],
        "forecasts": out,
        "min_days": MIN_DAYS,
        "note": "과거 평균 곡선을 현재 비율로 보정한 참고용 전망이에요. null은 근거 부족",
    }