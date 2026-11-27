"""직원 연차: 기준일(입사일)부터 1년마다 정해진 일수가 새로 생기고, 남은 건 넘어가지 않음.
직원은 [연차] 화면 달력에서 날짜를 눌러 신청, 원장은 리포트에서 승인·설정."""
from datetime import date, timedelta

from flask import render_template, request

from . import db
from .views import bp, ok, err, report_unlocked

KIND = {"full": ("연차", 1.0), "am": ("오전 반차", 0.5), "pm": ("오후 반차", 0.5)}


def _anniv(base, year):
    try:
        return base.replace(year=year)
    except ValueError:  # 2/29 기준일
        return base.replace(year=year, day=28)


def period(base, today=None):
    """today가 들어 있는 연차 기간 (시작, 끝)."""
    today = today or date.today()
    s = _anniv(base, today.year)
    if s > today:
        s = _anniv(base, today.year - 1)
    return s, _anniv(base, s.year + 1) - timedelta(days=1)


def staff_rows():
    out = []
    for r in db.q("SELECT * FROM staff WHERE active=1 ORDER BY id"):
        row = {"id": r["id"], "name": r["name"], "base": r["leave_base"] or "", "total": r["leave_days"]}
        if r["leave_base"] and r["leave_days"] is not None:
            s, e = period(date.fromisoformat(r["leave_base"]))
            used = db.one("SELECT COALESCE(SUM(CASE WHEN status='ok' THEN days END),0) AS u, COALESCE(SUM(CASE WHEN status='wait' THEN days END),0) AS w"
                          " FROM leaves WHERE staff_id=? AND date BETWEEN ? AND ?", (r["id"], s.isoformat(), e.isoformat()))
            row.update(start=s.isoformat(), end=e.isoformat(), used=used["u"], wait=used["w"],
                       left=round(r["leave_days"] - used["u"], 1), newSoon=(date.today() - s).days < 14)
        out.append(row)
    return out


def _leaves(where="1=1", args=()):
    return [dict(r) for r in db.q(f"SELECT l.*, s.name FROM leaves l JOIN staff s ON s.id=l.staff_id WHERE {where} ORDER BY l.date", args)]


@bp.route("/leave")
def leave():
    m = request.args.get("m") or date.today().strftime("%Y-%m")
    first = date.fromisoformat(m + "-01")
    nxt = (first + timedelta(days=32)).replace(day=1)
    return render_template("leave.html", data={
        "m": m, "prev": (first - timedelta(days=1)).strftime("%Y-%m"), "next": nxt.strftime("%Y-%m"),
        "today": date.today().isoformat(), "staff": staff_rows(),
        "leaves": _leaves("l.status IN ('ok','wait') AND l.date >= ? AND l.date < ?", (first.isoformat(), nxt.isoformat()))})


@bp.post("/api/leave/request")
def api_leave_request():
    j = request.get_json() or {}
    st = db.one("SELECT id FROM staff WHERE id=? AND active=1", (j.get("staff"),))
    if not st:
        return err("누가 쓰는지 골라주세요.")
    kind = j.get("kind") if j.get("kind") in KIND else "full"
    dates = sorted({d for d in j.get("dates") or [] if isinstance(d, str) and len(d) == 10})
    try:
        [date.fromisoformat(d) for d in dates]
    except ValueError:
        return err("날짜가 이상해요.")
    if not dates:
        return err("달력에서 쉴 날을 눌러 골라주세요.")
    dup = db.one(f"SELECT date FROM leaves WHERE staff_id=? AND status IN ('ok','wait') AND date IN ({','.join('?' * len(dates))})", (st["id"], *dates))
    if dup:
        return err(f"{dup['date']}은 이미 신청했어요.")
    grp = int(date.today().strftime("%Y%m%d")) * 100000 + (db.one("SELECT COALESCE(MAX(id),0) AS n FROM leaves")["n"] + 1)
    for d in dates:
        db.run("INSERT INTO leaves(staff_id, date, kind, days, status, reason, grp, created_at) VALUES (?,?,?,?,?,?,?,?)",
               (st["id"], d, kind, KIND[kind][1], "wait", (j.get("reason") or "").strip(), grp, db.now()))
    return ok(n=len(dates))


@bp.post("/api/leave/<int:lid>/cancel")
def api_leave_cancel(lid):
    """승인 전인 신청만 직원이 취소."""
    n = db.run("DELETE FROM leaves WHERE id=? AND status='wait'", (lid,)).rowcount
    return ok() if n else err("승인된 연차는 원장님이 리포트에서 바꿀 수 있어요.")


# ---- 원장 (리포트 안) ----
def admin_data():
    wait = _leaves("l.status='wait'")
    for w in wait:  # 같은 날 쉬는 다른 직원
        w["others"] = [r["name"] for r in db.q("SELECT s.name FROM leaves l JOIN staff s ON s.id=l.staff_id WHERE l.date=? AND l.staff_id<>? AND l.status IN ('ok','wait')",
                                               (w["date"], w["staff_id"]))]
    done = _leaves("l.status IN ('ok','no') AND l.decided_at >= ?", ((date.today() - timedelta(days=60)).isoformat(),))
    done.sort(key=lambda r: r["decided_at"] or "", reverse=True)
    return {"wait": wait, "done": done[:20], "staff": staff_rows(), "kinds": {k: v[0] for k, v in KIND.items()}}


@bp.post("/api/leave/decide")
def api_leave_decide():
    if not report_unlocked():
        return err("리포트 비밀번호로 다시 들어와 주세요.")
    j = request.get_json() or {}
    act = j.get("act")
    if act not in ("ok", "no", "wait"):
        return err("잘못된 요청이에요.")
    n = 0
    for i in j.get("ids") or []:
        n += db.run("UPDATE leaves SET status=?, decided_at=? WHERE id=?", (act, db.now() if act != "wait" else None, i)).rowcount
    return ok(n=n)


@bp.post("/api/leave/staff")
def api_leave_staff():
    if not report_unlocked():
        return err("리포트 비밀번호로 다시 들어와 주세요.")
    j = request.get_json() or {}
    base = (j.get("base") or "").strip()
    if base:
        try:
            date.fromisoformat(base)
        except ValueError:
            return err("기준일은 2025-03-02 처럼 적어주세요.")
    try:
        days = float(j["days"]) if str(j.get("days", "")).strip() != "" else None
    except ValueError:
        return err("연차 일수는 숫자로 적어주세요.")
    db.run("UPDATE staff SET leave_base=?, leave_days=? WHERE id=?", (base or None, days, j.get("id")))
    return ok()


if __name__ == "__main__":  # 기간 계산 자가 점검
    assert period(date(2023, 3, 2), date(2026, 11, 27)) == (date(2026, 3, 2), date(2027, 3, 1))
    assert period(date(2023, 3, 2), date(2027, 3, 1)) == (date(2026, 3, 2), date(2027, 3, 1))
    assert period(date(2023, 3, 2), date(2027, 3, 2))[0] == date(2027, 3, 2)
    assert period(date(2024, 2, 29), date(2025, 3, 1)) == (date(2025, 2, 28), date(2026, 2, 27))
    print("ok")
