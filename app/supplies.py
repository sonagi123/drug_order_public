"""비품 관리: 간식·사무용품·위생용품처럼 약이 아닌 물건의 재고, 구매 링크, 결재(요청 → 승인 → 구매 완료)."""
import json
from datetime import date
from urllib.parse import urlparse

from flask import render_template, request

from . import db
from .views import bp, ok, err

MALLS = [("coupang", "쿠팡"), ("smartstore.naver", "네이버"), ("shopping.naver", "네이버"), ("naver", "네이버"),
         ("11st", "11번가"), ("gmarket", "G마켓"), ("auction", "옥션"), ("ssg", "SSG"), ("kurly", "컬리"),
         ("lotteon", "롯데온"), ("tmon", "티몬"), ("wemakeprice", "위메프"), ("danawa", "다나와"), ("aliexpress", "알리")]
NO_LINK = "직접 구매"


def mall_of(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    for key, name in MALLS:
        if key in host:
            return name
    return host.removeprefix("www.").removeprefix("m.")


def clean_links(raw):
    out = []
    for u in raw or []:
        u = (u or "").strip()
        if not u:
            continue
        if not u.lower().startswith(("http://", "https://")):
            u = "https://" + u
        p = urlparse(u)
        if p.scheme in ("http", "https") and p.hostname:
            out.append(u)
    return out[:8]


def num(v, default=None):
    try:
        v = str(v).replace(",", "").strip()
        return float(v) if v != "" else default
    except (TypeError, ValueError):
        return default


def _row(r, reqs):
    links = json.loads(r["links"] or "[]")
    mall = r["mall"] or (mall_of(links[0]) if links else NO_LINK)
    pend = reqs.get(r["id"])
    last = db.one("SELECT at, delta FROM supply_log WHERE supply_id=? AND kind='in' AND undone=0 ORDER BY id DESC LIMIT 1", (r["id"],))
    return {"id": r["id"], "name": r["name"], "cat": r["cat"] or "", "unit": r["unit"] or "개", "qty": r["qty"] or 0,
            "min": r["min_qty"] or 0, "price": r["price"], "mall": mall, "mallSet": r["mall"] or "", "memo": r["memo"] or "",
            "links": [{"url": u, "mall": mall_of(u)} for u in links],
            "low": (r["min_qty"] or 0) > 0 and (r["qty"] or 0) <= (r["min_qty"] or 0),
            "req": pend, "lastIn": last["at"][:10] if last else None}


def _pending():
    """비품별 진행 중인 요청 (결재 대기 / 승인됨)."""
    out = {}
    for r in db.q("SELECT id, supply_id, qty, status FROM supply_reqs WHERE status IN ('wait','ok') ORDER BY id"):
        out.setdefault(r["supply_id"], {"status": r["status"], "qty": 0})
        out[r["supply_id"]]["qty"] += r["qty"]
        if r["status"] == "wait":
            out[r["supply_id"]]["status"] = "wait"
    return out


def cats():
    return [r["name"] for r in db.q("SELECT name FROM supply_cats ORDER BY sort, id")]


@bp.route("/supplies")
def supplies():
    reqs = _pending()
    rows = [_row(r, reqs) for r in db.q("SELECT * FROM supplies WHERE active=1 ORDER BY name")]
    names = {r["id"]: r["name"] for r in db.q("SELECT id, name FROM supplies")}
    units = {r["id"]: r["unit"] for r in db.q("SELECT id, unit FROM supplies")}
    log = [dict(r, name=names.get(r["supply_id"], "(지운 비품)"), unit=units.get(r["supply_id"], ""))
           for r in db.q("SELECT * FROM supply_log WHERE undone=0 ORDER BY id DESC LIMIT 15")]
    wait = db.one("SELECT COUNT(*) AS n FROM supply_reqs WHERE status='wait'")["n"]
    okn = db.one("SELECT COUNT(*) AS n FROM supply_reqs WHERE status='ok'")["n"]
    return render_template("supplies.html", data={"rows": rows, "cats": cats(), "log": log, "wait": wait, "okn": okn,
                                                  "malls": sorted({m for _, m in MALLS} | {NO_LINK, "도매상"})})


@bp.post("/api/supplies/save")
def api_supply_save():
    j = request.get_json() or {}
    name = (j.get("name") or "").strip()
    if not name:
        return err("이름을 적어주세요.")
    cat = (j.get("cat") or "").strip()
    if cat and not db.one("SELECT 1 FROM supply_cats WHERE name=?", (cat,)):
        db.run("INSERT INTO supply_cats(name, sort) VALUES (?, (SELECT COALESCE(MAX(sort),0)+1 FROM supply_cats))", (cat,))
    vals = (name, cat, (j.get("unit") or "개").strip() or "개", num(j.get("min"), 0), num(j.get("price")),
            (j.get("mall") or "").strip(), json.dumps(clean_links(j.get("links")), ensure_ascii=False), (j.get("memo") or "").strip(), db.now())
    sid = j.get("id")
    qty = num(j.get("qty"), 0)
    if sid:
        cur = db.one("SELECT qty FROM supplies WHERE id=?", (sid,))
        if not cur:
            return err("없는 비품이에요.")
        db.run("UPDATE supplies SET name=?, cat=?, unit=?, min_qty=?, price=?, mall=?, links=?, memo=?, updated_at=? WHERE id=?", (*vals, sid))
        if qty != (cur["qty"] or 0):  # 지금 재고를 고쳤으면 기록
            db.run("UPDATE supplies SET qty=? WHERE id=?", (qty, sid))
            db.run("INSERT INTO supply_log(supply_id, at, kind, delta, qty_after, who, note) VALUES (?,?,?,?,?,?,?)",
                   (sid, db.now(), "count", qty - (cur["qty"] or 0), qty, j.get("who") or "", "재고 고침"))
    else:
        sid = db.run("INSERT INTO supplies(name, cat, unit, min_qty, price, mall, links, memo, updated_at, qty, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (*vals, qty, db.now())).lastrowid
        db.run("INSERT INTO supply_log(supply_id, at, kind, delta, qty_after, who, note) VALUES (?,?,?,?,?,?,?)",
               (sid, db.now(), "count", qty, qty, j.get("who") or "", "새 비품"))
    return ok(id=sid)


@bp.post("/api/supplies/<int:sid>/delete")
def api_supply_delete(sid):
    db.run("UPDATE supplies SET active=0 WHERE id=?", (sid,))
    db.run("UPDATE supply_reqs SET status='no', decided_at=? WHERE supply_id=? AND status IN ('wait','ok')", (db.now(), sid))
    return ok()


def _adjust(sid, delta, kind, who="", note=""):
    cur = db.one("SELECT qty FROM supplies WHERE id=? AND active=1", (sid,))
    if not cur:
        return None
    after = round((cur["qty"] or 0) + delta, 2)
    if after < 0:
        after, delta = 0, -(cur["qty"] or 0)
    db.run("UPDATE supplies SET qty=?, updated_at=? WHERE id=?", (after, db.now(), sid))
    return db.run("INSERT INTO supply_log(supply_id, at, kind, delta, qty_after, who, note) VALUES (?,?,?,?,?,?,?)",
                  (sid, db.now(), kind, delta, after, who, note)).lastrowid, after


@bp.post("/api/supplies/<int:sid>/adjust")
def api_supply_adjust(sid):
    """썼어요(use: 빼기) / 들어왔어요(in: 더하기). 들어온 건 승인된 요청이 있으면 구매 완료로."""
    j = request.get_json() or {}
    q = num(j.get("qty"))
    if not q or q <= 0:
        return err("수량을 적어주세요.")
    kind = "in" if j.get("kind") == "in" else "use"
    note = ""
    if kind == "in":
        req = db.one("SELECT id, qty FROM supply_reqs WHERE supply_id=? AND status='ok' ORDER BY id LIMIT 1", (sid,))
        if req:
            db.run("UPDATE supply_reqs SET status='done', done_at=? WHERE id=?", (db.now(), req["id"]))
            note = f"결재 #{req['id']}"
    res = _adjust(sid, q if kind == "in" else -q, kind, j.get("who") or "", note)
    if not res:
        return err("없는 비품이에요.")
    return ok(log=res[0], qty=res[1])


@bp.post("/api/supplies/undo/<int:lid>")
def api_supply_undo(lid):
    lg = db.one("SELECT * FROM supply_log WHERE id=? AND undone=0 AND kind IN ('use','in')", (lid,))
    if not lg:
        return err("되돌릴 기록이 없어요.")
    cur = db.one("SELECT qty FROM supplies WHERE id=?", (lg["supply_id"],))
    db.run("UPDATE supplies SET qty=? WHERE id=?", (max(0, round((cur["qty"] or 0) - (lg["delta"] or 0), 2)), lg["supply_id"]))
    db.run("UPDATE supply_log SET undone=1 WHERE id=?", (lid,))
    if lg["note"] and lg["note"].startswith("결재 #"):
        db.run("UPDATE supply_reqs SET status='ok', done_at=NULL WHERE id=?", (int(lg["note"][4:]),))
    return ok()


@bp.post("/api/supplies/cat")
def api_supply_cat():
    """분류 추가 / 이름 바꾸기 / 지우기."""
    j = request.get_json() or {}
    act, name, new = j.get("act"), (j.get("name") or "").strip(), (j.get("new") or "").strip()
    if act == "add":
        if not name:
            return err("분류 이름을 적어주세요.")
        if db.one("SELECT 1 FROM supply_cats WHERE name=?", (name,)):
            return err("이미 있는 분류예요.")
        db.run("INSERT INTO supply_cats(name, sort) VALUES (?, (SELECT COALESCE(MAX(sort),0)+1 FROM supply_cats))", (name,))
    elif act == "rename":
        if not new:
            return err("새 이름을 적어주세요.")
        if db.one("SELECT 1 FROM supply_cats WHERE name=?", (new,)):
            return err("이미 있는 분류예요.")
        db.run("UPDATE supply_cats SET name=? WHERE name=?", (new, name))
        db.run("UPDATE supplies SET cat=? WHERE cat=?", (new, name))
    elif act == "delete":
        db.run("DELETE FROM supply_cats WHERE name=?", (name,))
        db.run("UPDATE supplies SET cat='' WHERE cat=?", (name,))
    return ok(cats=cats())


@bp.post("/api/supplies/request")
def api_supply_request():
    """살 것 결재 올리기: [{id, qty}, ...]"""
    j = request.get_json() or {}
    items = [(int(x["id"]), num(x.get("qty"))) for x in (j.get("items") or []) if x.get("id")]
    items = [(i, q) for i, q in items if q and q > 0]
    if not items:
        return err("살 수량을 적어주세요.")
    for sid, q in items:
        r = db.one("SELECT price FROM supplies WHERE id=? AND active=1", (sid,))
        if r:
            db.run("INSERT INTO supply_reqs(supply_id, qty, status, who, price, created_at) VALUES (?,?,?,?,?,?)",
                   (sid, q, "wait", j.get("who") or "", r["price"], db.now()))
    return ok(n=len(items))


# ------------------------------------------------------------------ 결재함
@bp.route("/supplies/approve")
def supplies_approve():
    tab = request.args.get("t", "wait")
    if tab not in ("wait", "ok", "done", "no"):
        tab = "wait"
    sup = {r["id"]: r for r in db.q("SELECT * FROM supplies")}
    order = "r.id" if tab in ("wait", "ok") else "COALESCE(r.done_at, r.decided_at) DESC"
    rows = []
    for r in db.q(f"SELECT r.* FROM supply_reqs r WHERE r.status=? ORDER BY {order} LIMIT 300", (tab,)):
        s = sup.get(r["supply_id"])
        if not s:
            continue
        links = json.loads(s["links"] or "[]")
        mall = s["mall"] or (mall_of(links[0]) if links else NO_LINK)
        rows.append({"id": r["id"], "sid": s["id"], "name": s["name"], "unit": s["unit"] or "개", "qty": r["qty"],
                     "now": s["qty"] or 0, "min": s["min_qty"] or 0, "price": r["price"] if r["price"] is not None else s["price"],
                     "who": r["who"] or "", "at": (r["created_at"] or "")[:16], "decided": (r["decided_at"] or "")[:16],
                     "done": (r["done_at"] or "")[:16], "mall": mall, "memo": s["memo"] or "",
                     "links": [{"url": u, "mall": mall_of(u)} for u in links]})
    counts = {k: db.one("SELECT COUNT(*) AS n FROM supply_reqs WHERE status=?", (k,))["n"] for k in ("wait", "ok")}
    m0 = date.today().replace(day=1).isoformat()
    month = db.one("SELECT COALESCE(SUM(qty*COALESCE(price,0)),0) AS s FROM supply_reqs WHERE status IN ('ok','done') AND decided_at>=?", (m0,))["s"]
    return render_template("supplies_approve.html", data={"tab": tab, "rows": rows, "counts": counts, "month": month})


@bp.post("/api/supplies/decide")
def api_supply_decide():
    """승인/반려/되돌리기: {ids:[...], act:'ok'|'no'|'wait', qty:{id: n}}"""
    j = request.get_json() or {}
    act = j.get("act")
    if act not in ("ok", "no", "wait"):
        return err("잘못된 요청이에요.")
    qtys = j.get("qty") or {}
    n = 0
    for rid in j.get("ids") or []:
        q = num(qtys.get(str(rid)))
        if q is not None and q > 0:
            db.run("UPDATE supply_reqs SET qty=? WHERE id=? AND status IN ('wait','ok')", (q, rid))
        n += db.run("UPDATE supply_reqs SET status=?, decided_at=? WHERE id=? AND status IN ('wait','ok','no')",
                    (act, db.now() if act != "wait" else None, rid)).rowcount
    return ok(n=n)
