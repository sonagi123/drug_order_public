"""재고 추정과 주문 제안 계산 (PROJECT.md 4장)."""
import math
from datetime import date, timedelta

from . import db


def d(s):
    return date.fromisoformat(s)


def root_map():
    """같은 제품으로 합친 코드 → 대표 코드."""
    return {r["code"]: r["alias_of"] for r in db.q("SELECT code, alias_of FROM drugs WHERE alias_of IS NOT NULL")}


def usage_state():
    """업로드된 사용량 기간 정보: 날짜별 커버 여부, 최근 일평균, 마지막 날짜."""
    roots = root_map()
    imports = db.q("SELECT * FROM usage_imports ORDER BY id")
    day_import = {}  # 날짜 -> import id (나중 업로드 우선)
    for im in imports:
        s, e = d(im["start"]), d(im["end"])
        cur = s
        while cur <= e:
            day_import[cur] = im["id"]
            cur += timedelta(days=1)
    lengths = {im["id"]: (d(im["end"]) - d(im["start"])).days + 1 for im in imports}
    last_end = max((d(im["end"]) for im in imports), default=None)

    # 약별 하루 사용량(기간별)
    rate = {}  # (import_id, code) -> qty/day
    for r in db.q("SELECT import_id, code, qty FROM usage"):
        key = (r["import_id"], roots.get(r["code"], r["code"]))
        rate[key] = rate.get(key, 0) + (r["qty"] or 0) / lengths.get(r["import_id"], 1)

    # 일평균: 마지막 날짜로부터 35일 안에 끝나는 기간들
    avg = {}
    if imports:
        recent = [im for im in imports if (last_end - d(im["end"])).days <= 35] or imports
        # 겹치는 날 중복 없이 계산
        days = {}
        for im in recent:
            cur = d(im["start"])
            while cur <= d(im["end"]):
                days[cur] = im["id"]
                cur += timedelta(days=1)
        ndays = len(days) or 1
        per_import_days = {}
        for iid in days.values():
            per_import_days[iid] = per_import_days.get(iid, 0) + 1
        for (iid, code), rt in rate.items():
            if iid in per_import_days:
                avg[code] = avg.get(code, 0) + rt * per_import_days[iid]
        avg = {k: v / ndays for k, v in avg.items()}
    return {"day_import": day_import, "rate": rate, "avg": avg, "last_end": last_end}


def psy_manual():
    """향정 재고 계산 방식: 'manual'(향정 관리에 적은 사용량) / 'excel'(처방 통계). 설정 → 향정 재고 계산."""
    return db.get_setting("psy_mode") != "excel"


def stats_used(st, code, after, upto):
    """처방 통계로 본 사용량 합계 (after < 날짜 <= upto, 통계 없는 날은 0)."""
    used = 0.0
    cur = d(after) + timedelta(days=1)
    end = d(upto)
    while cur <= end:
        iid = st["day_import"].get(cur)
        if iid is not None:
            used += st["rate"].get((iid, code), 0.0)
        cur += timedelta(days=1)
    return used


def psy_compare(codes, days=35):
    """향정: 처방 통계가 있는 최근 기간 동안 '향정 관리 기록 합계' vs '엑셀 처방량 합계'."""
    st = usage_state()
    if not st["last_end"]:
        return None
    lo = st["last_end"] - timedelta(days=days - 1)
    first = db.one("SELECT MIN(date) AS d FROM psy_usage")["d"]
    if not first:
        return None
    lo = max(lo, d(first))  # 향정 기록을 시작한 날부터만 비교
    covered = sorted(x for x in st["day_import"] if x >= lo)
    if not covered:
        return None
    cs = {x.isoformat() for x in covered}
    rec = {}
    for r in db.q("SELECT code, date, qty FROM psy_usage WHERE date BETWEEN ? AND ?", (covered[0].isoformat(), covered[-1].isoformat())):
        if r["date"] in cs:
            rec[r["code"]] = rec.get(r["code"], 0) + (r["qty"] or 0)
    logged = {r["date"] for r in db.q("SELECT DISTINCT date FROM psy_usage WHERE date BETWEEN ? AND ?", (covered[0].isoformat(), covered[-1].isoformat()))}
    out = {}
    for c in codes:
        xl = sum(st["rate"].get((st["day_import"][x], c), 0.0) for x in covered)
        out[c] = {"rec": rec.get(c, 0), "xl": round(xl, 2)}
    return {"from": covered[0].isoformat(), "to": covered[-1].isoformat(), "days": len(covered),
            "logged": len(logged & cs), "rows": out}


def latest_usage_end():
    r = db.one("SELECT MAX(end) AS e FROM usage_imports")
    return r["e"] if r and r["e"] else None


def drug_rows(include_inactive=False):
    """약 목록 + 계산값."""
    st = usage_state()
    today = date.today()
    roots = root_map()
    sql = "SELECT * FROM drugs WHERE alias_of IS NULL" + ("" if include_inactive else " AND active=1") + " ORDER BY maker, name"
    drugs = db.q(sql)
    aliases = {}
    for a, root in roots.items():
        aliases.setdefault(root, []).append(a)

    # 입고 확인 수량(정) - 날짜별
    rec = {}
    for r in db.q("SELECT code, date, packs*pack AS units FROM receipts"):
        rec.setdefault(roots.get(r["code"], r["code"]), []).append((r["date"], r["units"] or 0))
    # 입고 대기(주문했지만 아직 안 들어온 것 + 미송)
    pend = {}
    for r in db.q(
        """SELECT oi.code, oi.packs, oi.pack, oi.cancelled,
                  COALESCE((SELECT SUM(packs) FROM receipts WHERE item_id=oi.id), 0) AS got
           FROM order_items oi"""
    ):
        left = r["packs"] - r["got"] - r["cancelled"]
        if left > 0:
            c = roots.get(r["code"], r["code"])
            pend[c] = pend.get(c, 0) + left * r["pack"]

    # 향정: 하루 사용량 기록(향정 관리) - 처방 통계 대신 이걸로 뺌
    psy_use = {}
    for r in db.q("SELECT code, date, qty FROM psy_usage"):
        psy_use.setdefault(roots.get(r["code"], r["code"]), []).append((r["date"], r["qty"] or 0))

    manual = psy_manual()
    out = []
    for g in drugs:
        code = g["code"]
        daily = st["avg"].get(code, 0.0)
        pack = g["pack"] or 0
        target = g["target"]
        base_qty, base_date = g["base_qty"], g["base_date"]
        est = None
        estimated_days = 0
        if base_qty is not None and base_date and g["psy"] and manual:
            today_s = today.isoformat()
            used = sum(q for dt, q in psy_use.get(code, []) if base_date < dt <= today_s)
            got = sum(u for dt, u in rec.get(code, []) if dt > base_date)
            est = base_qty + got - used
        elif base_qty is not None and base_date:
            bd = d(base_date)
            used = 0.0
            cur = bd + timedelta(days=1)
            while cur <= today:
                iid = st["day_import"].get(cur)
                if iid is not None:
                    used += st["rate"].get((iid, code), 0.0)
                else:
                    estimated_days += 1  # 통계가 아직 없는 날: 재고에서 빼지 않음(추정 안 함)
                cur += timedelta(days=1)
            got = sum(u for dt, u in rec.get(code, []) if dt > base_date)
            est = base_qty + got - used
        pending = pend.get(code, 0)
        sugg = None
        if target is not None and pack > 0:
            have = (est if est is not None else target) + pending
            sugg = max(0, math.ceil((target - have) / pack - 1e-9))
        days_left = (est / daily) if (est is not None and daily > 0) else None
        out.append({
            **dict(g),
            "daily": daily, "est": est, "pending": pending, "sugg": sugg,
            "days_left": days_left, "estimated_days": estimated_days, "aliases": aliases.get(code, []),
        })
    return out


def suggest_target(daily, days):
    raw = daily * days
    if raw <= 0:
        return None
    return round(raw / 10) * 10 if raw >= 100 else math.ceil(raw)


def set_base_if_missing(code, qty):
    """목표를 처음 넣을 때 '오늘 재고 = 목표'로 시작. (오늘까지의 사용량은 이미 반영된 것으로 봄)"""
    g = db.one("SELECT base_qty FROM drugs WHERE code=?", (code,))
    if g and g["base_qty"] is None:
        db.run("UPDATE drugs SET base_qty=?, base_date=? WHERE code=?", (qty, db.today(), code))


def set_base(code, qty, on_date=None):
    db.run("UPDATE drugs SET base_qty=?, base_date=? WHERE code=?", (qty, on_date or db.today(), code))


def merge_duplicates():
    """청구코드가 같은 약(같은 제품, eghis 코드만 다름)을 하나로 합침."""
    groups = {}
    for r in db.q("SELECT code, claim_code, pack, target, active FROM drugs WHERE alias_of IS NULL AND claim_code IS NOT NULL AND claim_code != ''"):
        groups.setdefault(r["claim_code"], []).append(r)
    used = {r["code"]: r["q"] or 0 for r in db.q("SELECT code, SUM(qty) AS q FROM usage GROUP BY code")}
    merged = 0
    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: (r["target"] is not None or bool(r["pack"]), used.get(r["code"], 0)), reverse=True)
        root = rows[0]["code"]
        for r in rows[1:]:
            db.run("UPDATE drugs SET alias_of=? WHERE code=?", (root, r["code"]))
            db.run("UPDATE drugs SET alias_of=? WHERE alias_of=?", (root, r["code"]))
            db.run("DELETE FROM cart WHERE code=?", (r["code"],))
            merged += 1
    return merged


def psy_balance(code, upto, base_qty=None, base_date=None, st=None):
    """향정 약의 그날(upto) 진료 마감 후 잔량 = 기준 재고 + 입고 − 사용량. 기준이 그 뒤면 None.
    사용량은 설정에 따라 향정 관리 기록(수기) 또는 처방 통계(엑셀)."""
    if base_qty is None:
        g = db.one("SELECT base_qty, base_date FROM drugs WHERE code=?", (code,))
        base_qty, base_date = (g["base_qty"], g["base_date"]) if g else (None, None)
    if base_qty is None or not base_date or base_date > upto:
        return None
    codes = [code] + [r["code"] for r in db.q("SELECT code FROM drugs WHERE alias_of=?", (code,))]
    ph = ",".join("?" * len(codes))
    got = db.one(f"SELECT COALESCE(SUM(packs*pack),0) AS n FROM receipts WHERE code IN ({ph}) AND date>? AND date<=?",
                 (*codes, base_date, upto))["n"]
    if psy_manual():
        used = db.one("SELECT COALESCE(SUM(qty),0) AS n FROM psy_usage WHERE code=? AND date>? AND date<=?",
                      (code, base_date, upto))["n"]
    else:
        used = stats_used(st or usage_state(), code, base_date, upto)
    return base_qty + got - used
