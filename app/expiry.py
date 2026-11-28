"""바코드(GS1)로 약·사용기한 읽기, 사용기한 임박 계산.

- 입고 체크에서 찍으면: 받은 수량 +1통 (재고 반영), 사용기한·제조번호·도매상 기록
- 약품 관리에서 찍으면: 사용기한만 등록 (재고 수량은 그대로, 도매상 지정 없음)
- 임박 계산: 먼저 들어온 상자부터 쓴다고 보고, 지금 추정 재고를 최근 상자부터 채워 넣음.
  사용기한을 모르는 나머지(열린 통·카세트)는 가장 먼저 쓰는 것으로 봄.
"""
import calendar
import math
import re
from datetime import date, timedelta

from flask import render_template, request

from . import calc, db
from .views import bp, ok, err, wholesalers, unit_prices, price_of

SOON_DAYS = 90  # 사용기한 몇 일 전부터 보여줄지
VAR_AIS = ("10", "21")  # 길이가 정해지지 않은 항목: 제조번호, 일련번호
FIX_AIS = {"01": 14, "17": 6, "11": 6, "15": 6, "16": 6}
GS = "\x1d"


def _ymd(s):
    """YYMMDD → date. DD=00이면 그 달 말일."""
    y, m, d = 2000 + int(s[:2]), int(s[2:4]), int(s[4:6])
    return date(y, m, d or calendar.monthrange(y, m)[1])


def parse_gs1(raw):
    """GS1 바코드 글자 → {gtin, exp, lot, serial}. 구분 글자(GS)가 빠져도 읽음."""
    s = re.sub(r"^\][A-Za-z]\d", "", (raw or "").strip())  # 스캐너가 붙이는 ]d2 같은 머리말
    s = s.replace("(", "").replace(")", "")
    out = {}
    i = 0
    while i < len(s):
        if s[i] == GS:
            i += 1
            continue
        ai = s[i:i + 2]
        i += 2
        if ai in FIX_AIS:
            v = s[i:i + FIX_AIS[ai]]
            i += FIX_AIS[ai]
        elif ai in VAR_AIS:
            end = s.find(GS, i)
            if end < 0:
                end = len(s) if ("21" if ai == "10" else "10") in out else _guess_end(s, i, ai)
            v = s[i:end]
            i = end
        else:
            break  # 모르는 항목: 거기서 멈춤 (필요한 01·17은 보통 앞에 있음)
        out[ai] = v
    if len(out.get("01", "")) != 14 or not out["01"].isdigit():
        return None
    r = {"gtin": out["01"], "lot": out.get("10") or None, "serial": out.get("21") or None, "exp": None}
    try:
        if out.get("17"):
            r["exp"] = _ymd(out["17"]).isoformat()
    except ValueError:
        pass
    return r


def _guess_end(s, i, ai):
    """GS가 빠졌을 때 변수 항목의 끝 찾기: 남은 글자에서 다음 항목(10/21) 머리를 찾음.
    국내 의약품은 일련번호가 제조번호로 시작하는 경우가 많아 그 자리를 우선."""
    other = "21" if ai == "10" else "10"
    cands = [k for k in range(i + 1, len(s) - 1) if s[k:k + 2] == other]
    if not cands:
        return len(s)
    for k in cands:
        if s[k + 2:].startswith(s[i:k]):
            return k
    return cands[0]


def std13(gtin):
    return gtin[1:] if len(gtin) == 14 and gtin[0] == "0" else gtin


def _drug(code):
    g = db.one("SELECT code, name, order_name, pack, pack_label, unit, psy, alias_of FROM drugs WHERE code=?", (code,))
    if not g:
        return None
    if g["alias_of"]:
        return _drug(g["alias_of"])
    return {"code": g["code"], "name": g["order_name"] or g["name"], "pack": g["pack"] or 0,
            "label": g["pack_label"] or f"{g['pack'] or '?'}{g['unit'] or '정'}", "unit": g["unit"] or "정", "psy": g["psy"]}


def find_drug(gtin):
    """(약, 확인됨 여부). 처음 보는 바코드는 보험코드 앞 8자리로 후보를 찾음."""
    m = db.one("SELECT code FROM barcodes WHERE gtin=?", (gtin,))
    if m and _drug(m["code"]):
        return _drug(m["code"]), True
    s13 = std13(gtin)
    if len(s13) == 13 and s13.startswith("880"):
        c = db.one("SELECT code FROM drugs WHERE substr(claim_code,1,8)=? ORDER BY alias_of IS NOT NULL, active DESC LIMIT 1", (s13[3:11],))
        if c:
            return _drug(c["code"]), False
    return None, False


def norm_exp(v):
    """손으로 적은 사용기한: 2811 / 202811 / 281130 / 2028-11-30 → YYYY-MM-DD (연월만이면 말일)."""
    v = re.sub(r"[^\d]", "", v or "")
    try:
        if len(v) == 4:
            return _ymd(v + "00").isoformat()
        if len(v) == 6 and v.startswith("20"):
            return _ymd(v[2:] + "00").isoformat()
        if len(v) == 6:
            return _ymd(v).isoformat()
        if len(v) == 8:
            return date(int(v[:4]), int(v[4:6]), int(v[6:])).isoformat()
    except ValueError:
        return None
    return None


def add_boxes(code, boxes, exp_manual, packs, ws_id, receipt_id, src):
    """입고 저장 때: 찍은 상자 + (찍지 않은 나머지 통에 손으로 적은 기한)."""
    d = _drug(code)
    if not d:
        return
    n = 0
    for b in boxes or []:
        if not b.get("exp") or n >= packs:
            continue
        if b.get("serial") and db.one("SELECT 1 FROM boxes WHERE gtin=? AND serial=?", (b.get("gtin"), b["serial"])):
            continue
        db.run("INSERT INTO boxes(code, gtin, exp, lot, serial, qty, pack, ws_id, src, receipt_id, created_at) VALUES (?,?,?,?,?,1,?,?,?,?,?)",
               (d["code"], b.get("gtin"), b["exp"], b.get("lot"), b.get("serial"), d["pack"], ws_id, src, receipt_id, db.now()))
        n += 1
    rest = packs - n
    exp = norm_exp(exp_manual)
    if rest > 0 and exp:
        db.run("INSERT INTO boxes(code, exp, qty, pack, ws_id, src, receipt_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
               (d["code"], exp, rest, d["pack"], ws_id, src, receipt_id, db.now()))


# ------------------------------------------------------------------ 임박 계산
def lots(today=None):
    """사용기한 아는 묶음별 남은 양(추정)과 기한 넘겨 남을 양."""
    today = today or date.today()
    info = {r["code"]: r for r in calc.drug_rows(include_inactive=True)}
    by = {}
    for b in db.q("SELECT * FROM boxes WHERE status='ok'"):
        by.setdefault(b["code"], []).append(dict(b))
    out = []
    for code, bs in by.items():
        g = info.get(code)
        if not g:
            continue
        pack = g["pack"] or 0
        for b in bs:
            b["units"] = (b["qty"] or 0) * (b["pack"] or pack or 0)
        # 최근 들어온 상자부터 지금 재고를 채움 (먼저 들어온 것부터 쓰므로). 같은 날이면 기한 긴 것이 나중에 쓰임
        bs.sort(key=lambda b: (b["created_at"][:10], b["exp"]), reverse=True)
        left = g["est"] if g["est"] is not None else sum(b["units"] for b in bs)
        left = max(0.0, left)
        for b in bs:
            b["left"] = min(b["units"], left)
            left -= b["left"]
        ahead = left  # 기한 모르는 나머지(열린 통·카세트): 가장 먼저 씀
        daily = g["daily"] or 0
        for b in reversed(bs):  # 쓰는 순서대로
            if b["left"] <= 0:
                continue
            dte = (date.fromisoformat(b["exp"]) - today).days
            if dte < 0:
                b["over"] = b["left"]
            elif daily > 0:
                b["over"] = min(b["left"], max(0.0, ahead + b["left"] - daily * dte))
            else:
                b["over"] = b["left"]
            b["finish"] = (today + timedelta(days=math.ceil((ahead + b["left"]) / daily))).isoformat() if daily > 0 else None
            b["days"] = dte
            ahead += b["left"]
            out.append({**b, "name": g["order_name"] or g["name"], "unit": g["unit"] or "정", "psy": g["psy"],
                        "daily": daily, "pk": pack})
    # 같은 약·기한·제조번호·도매상·표시는 한 줄로
    rows = {}
    for b in out:
        k = (b["code"], b["exp"], b["lot"] or "", b["ws_id"], b["flag"] or "")
        r = rows.get(k)
        if r:
            r["left"] += b["left"]
            r["over"] += b["over"]
            r["ids"].append(b["id"])
            r["finish"] = max(r["finish"] or "", b["finish"] or "") or None
        else:
            rows[k] = {**b, "ids": [b["id"]]}
    return sorted(rows.values(), key=lambda r: (r["exp"], r["name"]))


def soon_rows():
    ws = {w["id"]: w["name"] for w in wholesalers(False)}
    rows = []
    for r in lots():
        if r["days"] > SOON_DAYS:
            continue
        r["state"] = "past" if r["days"] < 0 else "risk" if r["over"] >= 1 else "fine"
        r["ws"] = ws.get(r["ws_id"])
        r["boxesLeft"] = int(r["left"] // r["pk"]) if r["pk"] else 0
        rows.append(r)
    return rows


def alert_count():
    try:
        return sum(1 for r in soon_rows() if r["state"] != "fine")
    except Exception:  # 알림 때문에 주문 화면이 안 열리면 안 됨
        return 0


# ------------------------------------------------------------------ 화면·API
@bp.route("/expiry")
def expiry():
    t = request.args.get("t") or "soon"
    ws = wholesalers(False)
    data = {"t": t, "ws": [{"id": w["id"], "name": w["name"]} for w in ws], "today": db.today(), "soonDays": SOON_DAYS,
            "clinic": db.get_setting("clinic_name") or ""}
    rows = soon_rows()
    data["alert"] = sum(1 for r in rows if r["state"] != "fine")
    if t == "shelf":
        data["drugs"] = [{"code": r["code"], "name": r["order_name"] or r["name"]} for r in db.q("SELECT code, name, order_name FROM drugs WHERE active=1 AND alias_of IS NULL ORDER BY name")]
        data["shelf"] = shelf_today()
    else:
        data["rows"] = [{k: r[k] for k in ("code", "name", "unit", "psy", "exp", "lot", "ws", "ws_id", "flag", "left", "over",
                                           "finish", "days", "state", "ids", "boxesLeft", "src", "pk", "daily")} for r in rows]
        up = unit_prices()
        gone = [dict(r) for r in db.q("""SELECT b.*, COALESCE(NULLIF(d.order_name,''), d.name) AS name, d.unit FROM boxes b JOIN drugs d ON d.code=b.code
                                          WHERE b.status='gone' ORDER BY b.done_at DESC LIMIT 30""")]
        for g in gone:
            g["won"] = round((g["units_gone"] or 0) * price_of(up, g["code"]))
        data["gone"] = gone
        data["goneMonth"] = sum(g["won"] for g in gone if (g["done_at"] or "")[:7] == db.today()[:7])
    return render_template("expiry.html", data=data)


def shelf_today():
    info = {r["code"]: r for r in calc.drug_rows()}
    rows = {}
    for b in db.q("SELECT * FROM boxes WHERE src='shelf' AND status='ok' AND substr(created_at,1,10)=? ORDER BY id", (db.today(),)):
        g = info.get(b["code"])
        r = rows.setdefault(b["code"], {"code": b["code"], "name": (g["order_name"] or g["name"]) if g else b["code"], "unit": (g["unit"] if g else "정") or "정",
                                        "est": g["est"] if g else None, "units": 0, "boxes": 0, "lots": {}, "last": 0})
        r["units"] += (b["qty"] or 0) * (b["pack"] or 0)
        r["boxes"] += b["qty"] or 0
        r["lots"][b["exp"]] = r["lots"].get(b["exp"], 0) + (b["qty"] or 0)
        r["last"] = b["id"]
    out = sorted(rows.values(), key=lambda r: -r["last"])
    for r in out:
        r["lots"] = sorted(r["lots"].items())
    return out


@bp.get("/api/expiry/shelf_today")
def api_shelf_today():
    return ok(rows=shelf_today())


@bp.post("/api/barcode")
def api_barcode():
    """찍은 바코드 읽기. 저장은 하지 않음 (입고 체크는 [입고 확인 완료] 때 저장)."""
    p = parse_gs1((request.get_json() or {}).get("raw"))
    if not p:
        return err("약 바코드로 읽지 못했어요. 상자의 네모난 바코드를 찍어주세요.")
    d, linked = find_drug(p["gtin"])
    dup = bool(p["serial"] and db.one("SELECT 1 FROM boxes WHERE gtin=? AND serial=?", (p["gtin"], p["serial"])))
    return ok(**p, drug=d, linked=linked, dup=dup)


@bp.post("/api/barcode/link")
def api_barcode_link():
    j = request.get_json() or {}
    d = _drug(j.get("code"))
    if not d or not re.fullmatch(r"\d{14}", j.get("gtin") or ""):
        return err("잘못된 요청이에요.")
    db.run("INSERT OR REPLACE INTO barcodes(gtin, code, created_at) VALUES (?,?,?)", (j["gtin"], d["code"], db.now()))
    return ok(drug=d)


@bp.get("/api/barcode/search")
def api_barcode_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return ok(items=[])
    rows = db.q("""SELECT code FROM drugs WHERE alias_of IS NULL AND active=1 AND (name LIKE ? OR order_name LIKE ? OR maker LIKE ?)
                   ORDER BY name LIMIT 15""", (f"%{q}%",) * 3)
    return ok(items=[_drug(r["code"]) for r in rows])


@bp.post("/api/expiry/shelf")
def api_shelf_add():
    """약품 관리에서 찍은 기존 상자(또는 손으로 적은 것): 사용기한만 등록, 도매상 지정 없음."""
    j = request.get_json() or {}
    d = _drug(j.get("code"))
    exp = j.get("exp") if re.fullmatch(r"\d{4}-\d\d-\d\d", j.get("exp") or "") else norm_exp(j.get("exp"))
    if not d or not exp:
        return err("약과 사용기한을 확인해 주세요.")
    if j.get("serial") and db.one("SELECT 1 FROM boxes WHERE gtin=? AND serial=?", (j.get("gtin"), j["serial"])):
        return err("이미 찍은 상자예요.")
    qty = max(1, int(j.get("qty") or 1))
    bid = db.run("INSERT INTO boxes(code, gtin, exp, lot, serial, qty, pack, src, created_at) VALUES (?,?,?,?,?,?,?,'shelf',?)",
                 (d["code"], j.get("gtin"), exp, j.get("lot"), j.get("serial"), qty, d["pack"], db.now())).lastrowid
    return ok(id=bid, drug=d, exp=exp)


@bp.post("/api/expiry/<int:bid>/undo")
def api_shelf_undo(bid):
    n = db.run("DELETE FROM boxes WHERE id=? AND src='shelf' AND status='ok'", (bid,)).rowcount
    return ok() if n else err("되돌릴 수 없어요.")


@bp.post("/api/expiry/act")
def api_expiry_act():
    """first / unfirst / ws(도매상 지정) / gone(폐기·반품: 재고에서 뺌)."""
    j = request.get_json() or {}
    ids = [int(i) for i in j.get("ids") or []]
    act = j.get("act")
    if not ids:
        return err("고른 묶음이 없어요.")
    marks = ",".join("?" * len(ids))
    if act in ("first", "unfirst"):
        db.run(f"UPDATE boxes SET flag=? WHERE id IN ({marks})", ("first" if act == "first" else None, *ids))
        return ok()
    if act == "ws":
        db.run(f"UPDATE boxes SET ws_id=? WHERE id IN ({marks})", (j.get("ws"), *ids))
        return ok()
    if act == "gone":
        note = "반품" if j.get("why") == "반품" else "폐기"
        rows = {r["ids"][0]: r for r in lots()}
        sel = [r for r in rows.values() if set(r["ids"]) & set(ids)]
        if not sel:
            return err("이미 처리된 묶음이에요.")
        if any(r["psy"] for r in sel):
            return err("향정 약은 법정 절차가 있어 여기서 빼지 않아요. 향정 관리에서 재고를 고쳐 주세요.")
        est = {r["code"]: r["est"] for r in calc.drug_rows()}
        for r in sel:
            if est.get(r["code"]) is not None:
                calc.set_base(r["code"], max(0.0, est[r["code"]] - r["left"]))
                est[r["code"]] = max(0.0, est[r["code"]] - r["left"])
            db.run(f"UPDATE boxes SET status='gone', note=?, done_at=?, units_gone=CASE WHEN id=? THEN ? ELSE 0 END WHERE id IN ({','.join('?' * len(r['ids']))})",
                   (note, db.now(), r["ids"][0], r["left"], *r["ids"]))
        return ok(n=len(sel))
    return err("잘못된 요청이에요.")


if __name__ == "__main__":
    assert parse_gs1("010880649502022417280630102600702126007000000162") == {"gtin": "08806495020224", "lot": "260070", "serial": "26007000000162", "exp": "2028-06-30"}
    assert parse_gs1("0108806519049330172803231026006212600600108")["lot"] == "26006"
    assert parse_gs1("0108806519053931172709041026002212600201916") == {"gtin": "08806519053931", "lot": "26002", "serial": "2600201916", "exp": "2027-09-04"}
    assert parse_gs1("]d2010880649502022417280630102600702" + GS + "2126007000000162")["serial"] == "26007000000162"
    assert parse_gs1("0108806495020224172806001021A" + GS + "21XYZ")["lot"] == "21A" and parse_gs1("01088064950202241728060010X")["exp"] == "2028-06-30"
    assert parse_gs1("8806495020224") is None
    assert norm_exp("2811") == "2028-11-30" and norm_exp("2028-02") == "2028-02-29" and norm_exp("281115") == "2028-11-15"
    print("ok")
