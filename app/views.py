import io
import os
import socket
import threading
import time
from datetime import date, datetime, timedelta

from flask import (Blueprint, flash, jsonify, redirect, render_template, request, send_file, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import autoimport, calc, db, importers, maint

bp = Blueprint("main", __name__)
REPORT_TIMEOUT = 30 * 60


def wholesalers(active_only=True):
    sql = "SELECT * FROM wholesalers" + (" WHERE active=1" if active_only else "") + " ORDER BY sort, id"
    return [dict(r) for r in db.q(sql)]


def staff_list():
    return [r["name"] for r in db.q("SELECT name FROM staff WHERE active=1 ORDER BY id")]


def ok(**kw):
    return jsonify({"ok": True, **kw})


def err(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def usage_note():
    """추정 재고에 처방량이 며칠까지 반영됐는지 (화면 위 안내줄)."""
    end = calc.latest_usage_end()
    if not end:
        return None
    e, t = date.fromisoformat(end), date.fromisoformat(db.today())
    wd = "월화수목금토일"
    label = f"{e.month}/{e.day}({wd[e.weekday()]})"
    gap = (t - e).days
    n = e + timedelta(days=1)
    nxt = f"{n.month}/{n.day}({wd[n.weekday()]})"
    # 끝 날짜 당일에 내려받은 파일이면 그 시각까지만 들어 있음 (예: 월요일 오전 9시에 받으면 월요일 오전분까지)
    r = db.one("SELECT created_at FROM usage_imports WHERE end=? ORDER BY created_at DESC LIMIT 1", (end,))
    at = (r["created_at"] or "") if r else ""
    when = "하루 전체"
    if at[:10] == end and len(at) >= 16:
        h, mi = int(at[11:13]), at[14:16]
        when = f"{'오전' if h < 12 else '오후'} {h % 12 or 12}:{mi}까지"
    return {"end": end, "label": label, "when": when, "today": gap <= 0, "gap": max(0, gap), "next": nxt}


def setup_todo():
    """처음 설정에서 아직 안 한 것 (화면 위 안내줄용)."""
    todo = []
    if not (db.get_setting("clinic_name") or "").strip():
        todo.append(("병원 이름", "general"))
    if not db.one("SELECT 1 FROM wholesalers WHERE active=1"):
        todo.append(("도매상", "ws_add"))
    if not db.one("SELECT 1 FROM staff WHERE active=1"):
        todo.append(("직원 이름", "staff"))
    if not db.one("SELECT 1 FROM drugs"):
        todo.append(("eghis 파일 올리기", "upload"))
    return todo


def tour_steps():
    """시작 도우미 단계. done은 실제 데이터로 판단(도움말 '처음 설정'과 같은 순서)."""
    g = db.get_setting
    act = db.one("SELECT COUNT(*) AS n, SUM(CASE WHEN COALESCE(pack,0)>0 THEN 1 ELSE 0 END) AS p,"
                 " SUM(CASE WHEN target IS NOT NULL THEN 1 ELSE 0 END) AS t, SUM(CASE WHEN base_qty IS NOT NULL THEN 1 ELSE 0 END) AS b"
                 " FROM drugs WHERE active=1 AND alias_of IS NULL")
    n = act["n"] or 0
    most = lambda k: n > 0 and (act[k] or 0) >= n * 0.8
    st = url_for("main.settings")
    return [
        {"k": "name", "t": "병원 이름", "d": "화면 제목과 카톡 인사말에 들어가요.", "href": st + "#general",
         "done": bool((g("clinic_name") or "").strip())},
        {"k": "ws", "t": "도매상 추가", "d": "거래하는 도매상과 결제 마감일·할인율을 넣어요.", "href": st + "#ws_add",
         "done": bool(db.one("SELECT 1 FROM wholesalers WHERE active=1"))},
        {"k": "staff", "t": "직원 이름", "d": "입고 확인·향정 기록을 할 사람을 넣어요.", "href": st + "#staff",
         "done": bool(db.one("SELECT 1 FROM staff WHERE active=1"))},
        {"k": "pw", "t": "원장 비밀번호", "d": "리포트와 향정 재고 방식 변경에 써요.", "href": st + "#pw",
         "done": bool(g("report_pw"))},
        {"k": "backup", "t": "백업 폴더 (권장)", "d": "구글 드라이브 안의 폴더를 고르면 매일 자동 백업돼요.", "href": st + "#backup_now",
         "done": bool((g("backup_dir") or "").strip()), "opt": True},
        {"k": "watch", "t": "자동 읽기 폴더", "d": "eghis 엑셀을 저장할 폴더. 저장만 하면 5분 안에 읽어요.", "href": url_for("main.upload") + "#watch",
         "done": bool((g("watch_dir") or "").strip())},
        {"k": "stats", "t": "eghis 처방 통계 넣기", "d": "[통계] → [진료] 처방발생 통계 → 엑셀(F10). 최근 1~3개월, 파일 이름 끝에 기간(_20261101-20261128).",
         "href": url_for("main.upload"), "done": bool(db.one("SELECT 1 FROM usage_imports"))},
        {"k": "pack", "t": "포장단위 채우기", "d": "[포장 미입력] 약마다 1통이 몇 정인지 넣어요.", "href": url_for("main.drugs") + "#f=pack",
         "done": most("p")},
        {"k": "target", "t": "목표 재고", "d": "[빈 목표에 제안값 일괄 적용] → 3주치 목표가 들어가요.", "href": url_for("main.drugs") + "#f=target",
         "done": most("t")},
        {"k": "stock", "t": "지금 재고 입력", "d": "통 + 낱알로 적고, 기준일은 센 날(아침에 셌으면 전날).", "href": url_for("main.drugs") + "#stock",
         "done": n > 0 and (act["b"] or 0) > 0},
        {"k": "qr", "t": "폰·태블릿 접속", "d": "설정 → 접속 주소의 QR을 찍고 홈 화면에 추가해요.", "href": st,
         "done": g("tour_qr") == "1", "manual": "qr", "btn": "접속해 봤어요"},
        {"k": "pc", "t": "직원 PC에서 접속", "d": "직원 PC는 설치할 것 없이 크롬·엣지 주소창에 아래 주소를 넣으면 돼요. 같은 병원 인터넷(와이파이·랜)이어야 해요. 열리면 즐겨찾기(Ctrl+D)나 바탕화면 바로가기로 저장해 두세요.",
         "href": st, "done": g("tour_pc") == "1", "manual": "pc", "btn": "직원 PC에서 열어 봤어요", "addr": True},
    ]


@bp.app_context_processor
def ctx():
    state = db.get_setting("tour") or "done"
    tour = None
    if state != "done" or request.endpoint == "main.help_page":
        steps = tour_steps()
        ad = addresses()
        for x in steps:
            if x.get("addr"):
                x["addr"] = ad[1] if len(ad) > 1 else ad[0]
        tour = {"state": state, "steps": steps, "done": sum(1 for x in steps if x["done"]), "total": len(steps)}
    return {"clinic": db.get_setting("clinic_name"), "nav": request.endpoint, "usage_note": usage_note,
            "setup_todo": setup_todo(), "tour": tour}


@bp.post("/api/tour")
def api_tour():
    j = request.get_json(force=True) or {}
    if j.get("state") in ("open", "min", "done"):
        db.set_setting("tour", j["state"])
    for k in ("qr", "pc"):
        if k in j:
            db.set_setting("tour_" + k, "1" if j[k] else "")
    return jsonify(ok=True)


@bp.route("/help")
def help_page():
    return render_template("help.html")


@bp.route("/")
def home():
    return redirect(url_for("main.order"))


# ======================================================================== 주문
def _clinic_fill(t):
    """카톡 문구의 {병원이름} 자리에 설정의 병원 이름을 넣음."""
    return (t or "").replace("{병원이름}", db.get_setting("clinic_name") or "")


def message_for(ws, lines):
    header = _clinic_fill(ws.get("header") or db.get_setting("msg_header")).rstrip()
    fmt = ws.get("line_fmt") or db.get_setting("msg_line")
    footer = (ws.get("footer") or db.get_setting("msg_footer")).strip("\n")
    body = "\n".join(fmt.replace("{주문명}", n).replace("{통}", str(p)) for n, p in lines)
    return f"{header}\n\n{body}\n{footer}"


@bp.route("/order")
def order():
    from . import expiry
    rows = calc.drug_rows()
    cart = {r["code"]: dict(r) for r in db.q("SELECT * FROM cart")}
    items = []
    for r in rows:
        items.append({
            "code": r["code"], "name": r["order_name"] or r["name"], "full": r["name"], "maker": r["maker"] or "기타",
            "psy": r["psy"], "pack": r["pack"], "packLabel": r["pack_label"] or f"{r['pack']}{r['unit']}",
            "unit": r["unit"] or "정", "daily": r["daily"], "est": r["est"], "pending": r["pending"],
            "target": r["target"], "sugg": r["sugg"] or 0, "daysLeft": r["days_left"],
            "hasPack": bool(r["pack"]), "hasTarget": r["target"] is not None,
            "pref": r["pref_ws"], "insured": r["insured"], "price": r["price"], "buy": r["buy_price"],
        })
    not_ready = sum(1 for r in rows if not r["pack"])
    last_end = calc.latest_usage_end()
    est_days = (date.today() - date.fromisoformat(last_end)).days if last_end else None
    old_bo = db.one(
        """SELECT COUNT(*) AS n FROM order_items oi JOIN orders o ON o.id=oi.order_id
           WHERE oi.checked=1 AND o.order_date <= ? AND
           oi.packs - oi.cancelled - COALESCE((SELECT SUM(packs) FROM receipts WHERE item_id=oi.id),0) > 0""",
        ((date.today() - timedelta(days=7)).isoformat(),))["n"]
    wss = wholesalers()
    msg = {w["id"]: {"header": _clinic_fill(w["header"] or db.get_setting("msg_header")),
                     "line": w["line_fmt"] or db.get_setting("msg_line"),
                     "footer": w["footer"] or db.get_setting("msg_footer")} for w in wss}
    return render_template("order.html", data={
        "items": items, "cart": cart, "ws": wss, "msg": msg, "lastEnd": last_end, "estDays": est_days,
        "notReady": not_ready, "oldBackorders": old_bo, "expAlert": expiry.alert_count()})


@bp.post("/api/cart")
def cart_set():
    j = request.get_json()
    adds = j.get("items", [])
    for it in adds:
        db.run("INSERT INTO cart(code, ws_id, packs) VALUES (?,?,?) ON CONFLICT(code) DO UPDATE SET ws_id=excluded.ws_id, packs=excluded.packs",
               (it["code"], int(it["ws"]), max(0, int(it["packs"]))))
    return ok()


@bp.post("/api/cart/remove")
def cart_remove():
    db.run("DELETE FROM cart WHERE code=?", (request.get_json()["code"],))
    return ok()


@bp.post("/api/order/confirm")
def order_confirm():
    ws_id = int(request.get_json()["ws"])
    ws = db.one("SELECT * FROM wholesalers WHERE id=?", (ws_id,))
    rows = db.q("""SELECT c.code, c.packs, d.pack, COALESCE(NULLIF(d.order_name,''), d.name) AS oname
                   FROM cart c JOIN drugs d ON d.code=c.code WHERE c.ws_id=? AND c.packs>0 ORDER BY d.maker, d.name""", (ws_id,))
    if not rows:
        return err("장바구니가 비어 있어요.")
    nopack = [r["oname"] for r in rows if not r["pack"]]
    if nopack:
        return err("포장단위가 없는 약이 있어요: " + ", ".join(nopack))
    message = message_for(dict(ws), [(r["oname"], r["packs"]) for r in rows])
    conn = db.connect()
    cur = conn.execute("INSERT INTO orders(ws_id, order_date, created_at, message) VALUES (?,?,?,?)",
                       (ws_id, db.today(), db.now(), message))
    oid = cur.lastrowid
    for r in rows:
        conn.execute("INSERT INTO order_items(order_id, code, packs, pack) VALUES (?,?,?,?)",
                     (oid, r["code"], r["packs"], r["pack"]))
    conn.execute("DELETE FROM cart WHERE ws_id=?", (ws_id,))
    conn.commit()
    return ok(order=oid, count=len(rows))


@bp.route("/orders")
def orders_history():
    rows = db.q("""SELECT o.*, w.name AS ws, (SELECT COUNT(*) FROM order_items WHERE order_id=o.id) AS n
                   FROM orders o JOIN wholesalers w ON w.id=o.ws_id ORDER BY o.id DESC LIMIT 100""")
    return render_template("orders.html", rows=rows)


@bp.post("/api/order/<int:oid>/delete")
def order_delete(oid):
    if db.one("SELECT 1 FROM receipts r JOIN order_items oi ON oi.id=r.item_id WHERE oi.order_id=?", (oid,)):
        return err("이미 입고 확인한 품목이 있는 주문은 지울 수 없어요.")
    db.run("DELETE FROM order_items WHERE order_id=?", (oid,))
    db.run("DELETE FROM orders WHERE id=?", (oid,))
    return ok()


def _drop_empty_order(oid):
    if not db.one("SELECT 1 FROM order_items WHERE order_id=?", (oid,)):
        db.run("DELETE FROM orders WHERE id=?", (oid,))


@bp.post("/api/order/<int:oid>/delete_pending")
def order_delete_pending(oid):
    """입고 대기 중인 주문 삭제: 아직 확인 안 한 품목만 지움(이미 입고 확인한 품목 기록은 남김)."""
    n = db.run("""DELETE FROM order_items WHERE order_id=? AND checked=0
                   AND NOT EXISTS (SELECT 1 FROM receipts r WHERE r.item_id=order_items.id)""", (oid,)).rowcount
    _drop_empty_order(oid)
    return ok(count=n)


@bp.post("/api/order_item/<int:iid>/delete")
def order_item_delete(iid):
    """주문 품목 하나 삭제. 입고 기록이 있으면 그 기록도 지워져 추정 재고·리포트에서 빠짐."""
    it = db.one("SELECT * FROM order_items WHERE id=?", (iid,))
    if not it:
        return err("이미 지워진 품목이에요.")
    db.run("DELETE FROM receipts WHERE item_id=?", (iid,))
    db.run("DELETE FROM order_items WHERE id=?", (iid,))
    _drop_empty_order(it["order_id"])
    return ok()


# ======================================================================== 입고·미송
def _left_sql():
    return "oi.packs - oi.cancelled - COALESCE((SELECT SUM(packs) FROM receipts WHERE item_id=oi.id),0)"


@bp.route("/receive")
def receive():
    rows = db.q("""SELECT oi.id, oi.order_id, oi.code, oi.packs, oi.pack, o.order_date, o.ws_id, w.name AS ws,
                     COALESCE(NULLIF(d.order_name,''), d.name) AS name, d.pack_label, d.unit, COALESCE(d.alias_of, d.code) AS root
                    FROM order_items oi JOIN orders o ON o.id=oi.order_id JOIN wholesalers w ON w.id=o.ws_id
                    JOIN drugs d ON d.code=oi.code
                    WHERE oi.checked=0 ORDER BY o.order_date, o.id, d.maker, d.name""")
    orders = {}
    for r in rows:
        o = orders.setdefault(r["order_id"], {"id": r["order_id"], "ws": r["ws"], "wsId": r["ws_id"],
                                              "date": r["order_date"], "items": []})
        o["items"].append({"id": r["id"], "code": r["code"], "root": r["root"], "name": r["name"], "packs": r["packs"],
                           "pack": r["pack"], "label": r["pack_label"] or f"{r['pack']}{r['unit']}"})
    recent = db.q("""SELECT r.date, r.staff, w.name AS ws, COUNT(*) AS n, SUM(r.item_id IS NULL) AS extra
                     FROM receipts r LEFT JOIN wholesalers w ON w.id=r.ws_id
                     GROUP BY r.date, r.staff, r.ws_id ORDER BY r.date DESC, MAX(r.id) DESC LIMIT 12""")
    bo = db.one(f"SELECT COUNT(*) AS n FROM order_items oi WHERE oi.checked=1 AND {_left_sql()} > 0")["n"]
    drugs = [{"code": r["code"], "name": r["order_name"] or r["name"], "pack": r["pack"] or 1,
              "label": r["pack_label"] or ""} for r in db.q("SELECT * FROM drugs WHERE active=1 ORDER BY name")]
    return render_template("receive.html", data={
        "orders": list(orders.values()), "staff": staff_list(), "backorders": bo, "drugs": drugs,
        "ws": [{"id": w["id"], "name": w["name"]} for w in wholesalers()],
        "recent": [dict(r) for r in recent], "today": db.today()})


@bp.route("/receive/done")
def receive_done():
    """입고 완료: 모든 품목을 확인한 주문 (미송이 남았으면 표시)."""
    month = request.args.get("m") or db.today()[:7]
    months = [r["m"] for r in db.q("""SELECT DISTINCT substr(o.order_date,1,7) AS m FROM orders o
        WHERE NOT EXISTS (SELECT 1 FROM order_items x WHERE x.order_id=o.id AND x.checked=0) ORDER BY m DESC""")]
    if month not in months and months:
        months.append(month); months.sort(reverse=True)
    rows = db.q("""SELECT o.id AS oid, o.order_date, w.name AS ws, oi.id, oi.packs, oi.cancelled,
                     COALESCE(NULLIF(d.order_name,''), d.name) AS name,
                     COALESCE((SELECT SUM(packs) FROM receipts WHERE item_id=oi.id),0) AS got,
                     (SELECT MIN(date) FROM receipts WHERE item_id=oi.id) AS first_date,
                     (SELECT MAX(date) FROM receipts WHERE item_id=oi.id) AS last_date,
                     (SELECT GROUP_CONCAT(DISTINCT staff) FROM receipts WHERE item_id=oi.id) AS staff
                    FROM orders o JOIN wholesalers w ON w.id=o.ws_id JOIN order_items oi ON oi.order_id=o.id
                    JOIN drugs d ON d.code=oi.code
                    WHERE substr(o.order_date,1,7)=?
                      AND NOT EXISTS (SELECT 1 FROM order_items x WHERE x.order_id=o.id AND x.checked=0)
                    ORDER BY o.order_date DESC, o.id DESC, d.maker, d.name""", (month,))
    items = []
    for r in rows:
        items.append({"id": r["id"], "ws": r["ws"], "order": r["order_date"], "recv": r["first_date"], "late": r["last_date"] if r["first_date"] != r["last_date"] else None,
                      "name": r["name"], "packs": r["packs"], "got": r["got"], "cancelled": r["cancelled"],
                      "left": max(0, r["packs"] - r["cancelled"] - r["got"]), "staff": r["staff"] or ""})
    bo = db.one(f"SELECT COUNT(*) AS n FROM order_items oi WHERE oi.checked=1 AND {_left_sql()} > 0")["n"]
    waiting = db.one("SELECT COUNT(DISTINCT order_id) AS n FROM order_items WHERE checked=0")["n"]
    return render_template("receive_done.html", data={"items": items, "month": month, "today": db.today(), "months": months or [month],
                                                       "backorders": bo, "waiting": waiting})


@bp.post("/api/receive")
def receive_save():
    from . import expiry
    j = request.get_json()
    staff = (j.get("staff") or "").strip()
    if not staff:
        return err("체크하는 사람을 골라주세요.")
    day = j.get("date") or db.today()
    conn = db.connect()
    n = short = 0
    for it in j.get("items", []):
        oi = db.one("SELECT oi.*, o.ws_id FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE oi.id=?", (it["id"],))
        if not oi or oi["checked"]:
            continue
        got = max(0, int(it["packs"]))
        if got:
            rid = conn.execute("""INSERT INTO receipts(item_id, code, ws_id, packs, pack, date, staff, created_at)
                            VALUES (?,?,?,?,?,?,?,?)""", (oi["id"], oi["code"], oi["ws_id"], got, oi["pack"], day, staff, db.now())).lastrowid
            expiry.add_boxes(oi["code"], it.get("boxes"), it.get("exp"), got, oi["ws_id"], rid, "in")
        conn.execute("UPDATE order_items SET checked=1 WHERE id=?", (oi["id"],))
        short += max(0, oi["packs"] - got)
        n += 1
    for ex in j.get("extras", []):
        g = db.one("SELECT pack FROM drugs WHERE code=?", (ex["code"],))
        if not g or int(ex["packs"]) <= 0:
            continue
        rid = conn.execute("""INSERT INTO receipts(item_id, code, ws_id, packs, pack, date, staff, note, created_at)
                        VALUES (NULL,?,?,?,?,?,?,'주문 외 입고',?)""",
                     (ex["code"], ex.get("ws"), int(ex["packs"]), g["pack"] or 1, day, staff, db.now())).lastrowid
        expiry.add_boxes(ex["code"], ex.get("boxes"), ex.get("exp"), int(ex["packs"]), ex.get("ws"), rid, "in")
        n += 1
    conn.commit()
    return ok(count=n, short=short)


@bp.route("/backorder")
def backorder():
    rows = db.q(f"""SELECT oi.id, oi.packs, oi.pack, o.order_date, w.name AS ws, w.id AS ws_id,
                     COALESCE(NULLIF(d.order_name,''), d.name) AS name, d.pack_label, d.unit,
                     COALESCE((SELECT SUM(packs) FROM receipts WHERE item_id=oi.id),0) AS got, oi.cancelled
                    FROM order_items oi JOIN orders o ON o.id=oi.order_id JOIN wholesalers w ON w.id=o.ws_id
                    JOIN drugs d ON d.code=oi.code
                    WHERE oi.checked=1 AND {_left_sql()} > 0 ORDER BY o.order_date, d.name""")
    today = date.today()
    items = [{"id": r["id"], "name": r["name"], "ws": r["ws"], "date": r["order_date"],
              "age": (today - date.fromisoformat(r["order_date"])).days, "ordered": r["packs"], "got": r["got"],
              "left": r["packs"] - r["got"] - r["cancelled"], "label": r["pack_label"] or f"{r['pack']}{r['unit']}"}
             for r in rows]
    return render_template("backorder.html", data={"items": items, "staff": staff_list(),
                                                   "ws": [w["name"] for w in wholesalers(False)], "today": db.today()})


@bp.post("/api/backorder/confirm")
def backorder_confirm():
    j = request.get_json()
    staff = (j.get("staff") or "").strip()
    if not staff:
        return err("체크하는 사람을 골라주세요.")
    conn = db.connect()
    n = 0
    for it in j.get("items", []):
        oi = db.one(f"SELECT oi.*, o.ws_id, {_left_sql()} AS left FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE oi.id=?",
                    (it["id"],))
        if not oi:
            continue
        got = min(max(0, int(it["packs"])), oi["left"])
        if got:
            conn.execute("""INSERT INTO receipts(item_id, code, ws_id, packs, pack, date, staff, note, created_at)
                            VALUES (?,?,?,?,?,?,?,'미송 입고',?)""",
                         (oi["id"], oi["code"], oi["ws_id"], got, oi["pack"], j.get("date") or db.today(), staff, db.now()))
            n += 1
    conn.commit()
    return ok(count=n)


@bp.post("/api/backorder/cancel")
def backorder_cancel():
    iid = int(request.get_json()["id"])
    oi = db.one(f"SELECT oi.*, {_left_sql()} AS left FROM order_items oi WHERE oi.id=?", (iid,))
    if not oi or oi["left"] <= 0:
        return err("이미 처리된 품목이에요.")
    db.run("UPDATE order_items SET cancelled = cancelled + ? WHERE id=?", (oi["left"], iid))
    return ok()


@bp.route("/receive/export")
def receive_export():
    t = date.fromisoformat(db.today())
    a = request.args.get("from") or t.replace(day=1).isoformat()
    b = request.args.get("to") or t.isoformat()
    if a > b:
        a, b = b, a
    lm_end = t.replace(day=1) - timedelta(days=1)
    first = db.one("SELECT MIN(order_date) AS d FROM orders")["d"]
    st = db.one(f"""SELECT COUNT(DISTINCT o.id) AS orders, COUNT(oi.id) AS items,
                           SUM(CASE WHEN oi.checked=0 THEN 1 ELSE 0 END) AS waiting,
                           SUM(CASE WHEN oi.checked=1 AND {_left_sql()} > 0 THEN 1 ELSE 0 END) AS short
                    FROM orders o JOIN order_items oi ON oi.order_id=o.id WHERE o.order_date BETWEEN ? AND ?""", (a, b))
    nrec = db.one("SELECT COUNT(*) AS n FROM receipts WHERE date BETWEEN ? AND ?", (a, b))["n"]
    quick = [("이번 달", t.replace(day=1).isoformat(), t.isoformat()),
             ("지난 달", lm_end.replace(day=1).isoformat(), lm_end.isoformat()),
             ("최근 3개월", (t - timedelta(days=91)).isoformat(), t.isoformat())]
    if first:
        quick.append(("처음부터", first, t.isoformat()))
    bo = db.one(f"SELECT COUNT(*) AS n FROM order_items oi WHERE oi.checked=1 AND {_left_sql()} > 0")["n"]
    return render_template("receive_export.html", data={"from": a, "to": b, "today": t.isoformat(), "quick": quick,
                                                        "st": dict(st), "nrec": nrec, "backorders": bo})


@bp.route("/receipts.xlsx")
def receipts_xlsx():
    """입고 엑셀: 주문한 약 한 줄씩(주문일·약품·주문/받음/미송/취소·상태·입고일·확인자) + 입고 기록 시트."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    a = request.args.get("from") or db.today()[:8] + "01"
    b = request.args.get("to") or db.today()
    if a > b:
        a, b = b, a
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "주문별 입고 현황"
    head = ["주문일", "도매상", "약품", "포장(1통)", "주문(통)", "받음(통)", "미송 여부", "남은 미송(통)", "취소(통)", "상태",
            "첫 입고일", "마지막 입고일", "확인자"]
    ws.append(head)
    amber = PatternFill("solid", fgColor="FFF1D6")
    for r in db.q("""SELECT o.order_date, w.name AS ws, COALESCE(NULLIF(d.order_name,''), d.name) AS name, oi.pack, oi.packs,
                            oi.cancelled, oi.checked,
                            COALESCE((SELECT SUM(packs) FROM receipts WHERE item_id=oi.id),0) AS got,
                            (SELECT MIN(date) FROM receipts WHERE item_id=oi.id) AS d1,
                            (SELECT MAX(date) FROM receipts WHERE item_id=oi.id) AS d2,
                            (SELECT GROUP_CONCAT(DISTINCT staff) FROM receipts WHERE item_id=oi.id) AS staff
                     FROM order_items oi JOIN orders o ON o.id=oi.order_id JOIN wholesalers w ON w.id=o.ws_id
                     LEFT JOIN drugs d ON d.code=oi.code
                     WHERE o.order_date BETWEEN ? AND ?
                     ORDER BY o.order_date DESC, o.id DESC, d.maker, d.name""", (a, b)):
        left = max(0, r["packs"] - r["cancelled"] - r["got"])
        late = bool(r["d1"] and r["d2"] and r["d1"] != r["d2"])
        short = "" if not r["checked"] else ("있음" if (left or r["cancelled"] or late) else "없음")
        if not r["checked"]:
            state, left = "입고 대기", None
        elif left:
            state = "미송 남음"
        elif r["cancelled"]:
            state = "미송 취소" if r["got"] else "전부 취소"
        elif late:
            state = "모두 입고(미송 후 입고)"
        else:
            state = "모두 입고"
        ws.append([r["order_date"], r["ws"], r["name"], r["pack"], r["packs"], r["got"], short, left, r["cancelled"], state,
                   r["d1"] or "", r["d2"] if r["d2"] != r["d1"] else "", r["staff"] or ""])
        if left:
            for c in ws[ws.max_row]:
                c.fill = amber
    ws2 = wb.create_sheet("입고 기록")
    ws2.append(["입고일", "도매상", "약품", "통", "포장", "정", "확인자", "주문일", "비고"])
    for r in db.q("""SELECT r.*, w.name AS ws, COALESCE(NULLIF(d.order_name,''), d.name) AS name, o.order_date
                     FROM receipts r LEFT JOIN wholesalers w ON w.id=r.ws_id LEFT JOIN drugs d ON d.code=r.code
                     LEFT JOIN order_items oi ON oi.id=r.item_id LEFT JOIN orders o ON o.id=oi.order_id
                     WHERE r.date BETWEEN ? AND ? ORDER BY r.date DESC, r.id DESC""", (a, b)):
        ws2.append([r["date"], r["ws"], r["name"], r["packs"], r["pack"], r["packs"] * r["pack"], r["staff"],
                    r["order_date"] or "주문 외", r["note"] or ""])
    for sh in (ws, ws2):
        for c in sh[1]:
            c.font = Font(bold=True)
        sh.freeze_panes = "A2"
        sh.column_dimensions["C"].width = 30
        sh.column_dimensions["B"].width = 12
    ws.column_dimensions["J"].width = 22
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, download_name=f"입고현황_{a}_{b}.xlsx", as_attachment=True)


# ======================================================================== 향정 관리
def _psy_drugs():
    return db.q("SELECT * FROM drugs WHERE psy=1 AND active=1 AND alias_of IS NULL")


@bp.route("/psy")
def psy():
    day = request.args.get("d") or db.today()
    try:
        D0 = date.fromisoformat(day)
    except ValueError:
        D0 = date.fromisoformat(db.today())
    if D0 > date.fromisoformat(db.today()):
        D0 = date.fromisoformat(db.today())
    day = D0.isoformat()
    prev_day = (D0 - timedelta(days=1)).isoformat()
    week = [(D0 - timedelta(days=k)).isoformat() for k in range(6, -1, -1)]
    ST = calc.usage_state()
    avg = ST["avg"]
    manual = calc.psy_manual()
    rows = []
    for g in _psy_drugs():
        code = g["code"]
        aliases = [r["code"] for r in db.q("SELECT code FROM drugs WHERE alias_of=?", (code,))]
        ph = ",".join("?" * (1 + len(aliases)))
        rin = db.one(f"SELECT COALESCE(SUM(packs*pack),0) AS n FROM receipts WHERE code IN ({ph}) AND date=?", (code, *aliases, day))["n"]
        u = db.one("SELECT qty, staff FROM psy_usage WHERE code=? AND date=?", (code, day))
        hist = {r["date"]: r["qty"] for r in db.q("SELECT date, qty FROM psy_usage WHERE code=? AND date BETWEEN ? AND ?",
                                                    (code, week[0], week[-1]))}
        based_today = bool(g["base_date"] and g["base_date"] >= day)
        rows.append({
            "code": code, "name": importers.short_name(g["name"]), "pack": g["pack"] or 0, "unit": g["unit"] or "정",
            "prev": calc.psy_balance(code, prev_day, g["base_qty"], g["base_date"], st=ST),
            "in": rin, "qty": u["qty"] if u else None, "staff": u["staff"] if u else "",
            "usual": round(avg.get(code, 0)), "baseToday": based_today,
            "baseQty": g["base_qty"] if based_today else None,
            "week": [hist.get(w) for w in week],
            "now": calc.psy_balance(code, day, g["base_qty"], g["base_date"], st=ST),
            "xl": None if manual else (round(calc.stats_used(ST, code, prev_day, day), 1) if D0 in ST["day_import"] else None),
        })
    rows.sort(key=lambda r: r["name"])
    staff = [r["name"] for r in db.q("SELECT name FROM staff ORDER BY id")]
    first = db.one("SELECT MIN(date) AS d FROM psy_usage")["d"]
    cmp = None if manual else calc.psy_compare([r["code"] for r in rows])
    if cmp:
        cmp["rows"] = [{"name": r["name"], "unit": r["unit"], **cmp["rows"][r["code"]]} for r in rows]
    xl_avg = False
    if not manual and D0 in ST["day_import"]:
        im = db.one("SELECT start, end FROM usage_imports WHERE id=?", (ST["day_import"][D0],))
        xl_avg = bool(im and im["start"] != im["end"])
    return render_template("psy.html", data={"day": day, "today": db.today(), "rows": rows, "staff": staff,
                                             "mode": "manual" if manual else "excel", "cmp": cmp, "xlAvg": xl_avg,
                                             "week": week, "first": first, "monthStart": db.today()[:8] + "01"})


@bp.route("/psy/review")
def psy_review():
    """향정 기록 보기: 기간 안의 날짜별 사용량·입고·잔량·기록자."""
    b = request.args.get("to") or db.today()
    a = request.args.get("from") or (date.fromisoformat(b) - timedelta(days=13)).isoformat()
    if a > b:
        a, b = b, a
    drugs = {g["code"]: g for g in _psy_drugs()}
    ST = calc.usage_state()
    names = {c: importers.short_name(g["name"]) for c, g in drugs.items()}
    alias = {r["code"]: r["alias_of"] for r in db.q("SELECT code, alias_of FROM drugs WHERE alias_of IS NOT NULL")}
    by_day = {}
    for r in db.q("SELECT * FROM psy_usage WHERE date BETWEEN ? AND ?", (a, b)):
        if r["code"] in drugs:
            by_day.setdefault(r["date"], {}).setdefault(r["code"], {})["use"] = r["qty"]
            by_day[r["date"]][r["code"]]["staff"] = r["staff"]
    for r in db.q("SELECT code, date, SUM(packs*pack) AS n FROM receipts WHERE date BETWEEN ? AND ? GROUP BY code, date", (a, b)):
        c = alias.get(r["code"], r["code"])
        if c in drugs:
            e = by_day.setdefault(r["date"], {}).setdefault(c, {})
            e["in"] = (e.get("in") or 0) + (r["n"] or 0)
    days = []
    for dd in sorted(by_day, reverse=True):
        prev_d = (date.fromisoformat(dd) - timedelta(days=1)).isoformat()
        rows = []
        for c, e in by_day[dd].items():
            g = drugs[c]
            rows.append({"code": c, "name": names[c], "unit": g["unit"] or "정", "pack": g["pack"] or 0,
                         "prev": calc.psy_balance(c, prev_d, g["base_qty"], g["base_date"], st=ST),
                         "in": e.get("in") or 0, "use": e.get("use"), "staff": e.get("staff") or "",
                         "left": calc.psy_balance(c, dd, g["base_qty"], g["base_date"], st=ST),
                         "base": g["base_date"] == dd})
        rows.sort(key=lambda r: r["name"])
        staff = sorted({r["staff"] for r in rows if r["staff"]})
        days.append({"date": dd, "rows": rows, "staff": staff, "n": sum(1 for r in rows if r["use"] is not None),
                     "total": sum(r["use"] or 0 for r in rows), "ins": sum(1 for r in rows if r["in"])})
    return render_template("psy_review.html", data={"from": a, "to": b, "today": db.today(), "days": days,
                                                    "total": len(drugs), "first": db.one("SELECT MIN(date) AS d FROM psy_usage")["d"]})


@bp.post("/api/psy/base_morning")
def api_psy_base_morning():
    """그날 입력한 향정 재고를 '그날 아침(진료 전) 재고' = 전날 마감 재고로 옮김. 그래야 그날 사용량을 적을 수 있음."""
    day = (request.get_json() or {}).get("date") or db.today()
    prev = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    codes = [g["code"] for g in _psy_drugs() if g["base_date"] == day]
    conn = db.connect()
    for c in codes:
        conn.execute("UPDATE drugs SET base_date=? WHERE code=?", (prev, c))
        conn.execute("UPDATE counts SET date=? WHERE code=? AND date=?", (prev, c, day))
    conn.commit()
    return ok(count=len(codes))


@bp.post("/api/psy/save")
def api_psy_save():
    j = request.get_json()
    day, staff = j.get("date") or db.today(), (j.get("staff") or "").strip()
    if not staff:
        return err("기록하는 사람을 골라주세요.")
    if day > db.today():
        return err("미래 날짜에는 적을 수 없어요.")
    conn = db.connect()
    n = 0
    for it in j.get("items", []):
        code, v = it.get("code"), it.get("qty")
        if v is None or str(v).strip() == "":
            conn.execute("DELETE FROM psy_usage WHERE code=? AND date=?", (code, day))
            continue
        try:
            q = float(str(v).replace(",", ""))
        except ValueError:
            return err(f"숫자가 아닌 값이 있어요: {v}")
        if q < 0:
            return err("사용량은 0 이상이어야 해요.")
        conn.execute("""INSERT INTO psy_usage(code, date, qty, staff, created_at) VALUES (?,?,?,?,?)
                        ON CONFLICT(code, date) DO UPDATE SET qty=excluded.qty, staff=excluded.staff, created_at=excluded.created_at""",
                     (code, day, q, staff, db.now()))
        n += 1
    conn.commit()
    return ok(count=n)


@bp.route("/psy.xlsx")
def psy_xlsx():
    import openpyxl
    from openpyxl.styles import Font
    a = request.args.get("from") or db.today()[:8] + "01"
    b = request.args.get("to") or db.today()
    if a > b:
        a, b = b, a
    drugs = sorted(_psy_drugs(), key=lambda g: importers.short_name(g["name"]))
    ST = calc.usage_state()
    days = []
    cur = date.fromisoformat(a)
    while cur <= date.fromisoformat(b):
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "날짜별 기록"
    ws1.append(["날짜", "약품", "전날 잔량", "입고", "사용량", "잔량", "기록자"])
    for c in ws1[1]:
        c.font = Font(bold=True)
    ws2 = wb.create_sheet("약별 사용량")
    ws2.append(["약품"] + days + ["합계", f"{b} 잔량"])
    for c in ws2[1]:
        c.font = Font(bold=True)
    for g in drugs:
        code, name = g["code"], importers.short_name(g["name"])
        aliases = [r["code"] for r in db.q("SELECT code FROM drugs WHERE alias_of=?", (code,))]
        ph = ",".join("?" * (1 + len(aliases)))
        use = {r["date"]: r for r in db.q("SELECT * FROM psy_usage WHERE code=? AND date BETWEEN ? AND ?", (code, a, b))}
        rin = {r["date"]: r["n"] for r in db.q(f"""SELECT date, SUM(packs*pack) AS n FROM receipts WHERE code IN ({ph})
                                                  AND date BETWEEN ? AND ? GROUP BY date""", (code, *aliases, a, b))}
        line, tot = [name], 0
        for dd in days:
            u = use.get(dd)
            if u or rin.get(dd):
                prev = calc.psy_balance(code, (date.fromisoformat(dd) - timedelta(days=1)).isoformat(), g["base_qty"], g["base_date"], st=ST)
                ws1.append([dd, name, prev, rin.get(dd) or 0, u["qty"] if u else None,
                            calc.psy_balance(code, dd, g["base_qty"], g["base_date"], st=ST), u["staff"] if u else ""])
            line.append(u["qty"] if u else None)
            tot += u["qty"] if u else 0
        ws2.append(line + [tot, calc.psy_balance(code, b, g["base_qty"], g["base_date"], st=ST)])
    ws1.column_dimensions["B"].width = 26
    ws2.column_dimensions["A"].width = 26
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, download_name=f"향정기록_{a}_{b}.xlsx", as_attachment=True)


# ======================================================================== 약품 관리
@bp.route("/drugs")
def drugs():
    days = int(db.get_setting("default_days") or 21)
    rows = []
    hist = {r["code"] for r in db.q("SELECT DISTINCT code FROM order_items UNION SELECT DISTINCT code FROM receipts")}
    for r in calc.drug_rows(include_inactive=True):
        rows.append({
            "code": r["code"], "name": importers.short_name(r["name"]), "full": r["name"], "orderName": r["order_name"] or "", "maker": r["maker"] or "",
            "aliases": len(r["aliases"]),
            "psy": r["psy"], "pack": r["pack"] or 0, "packLabel": r["pack_label"] or "", "packSrc": r["pack_src"] or "",
            "unit": r["unit"] or "정", "daily": r["daily"], "target": r["target"], "est": r["est"],
            "sugg": calc.suggest_target(r["daily"], days), "insured": r["insured"], "price": r["price"],
            "buy": r["buy_price"], "pref": r["pref_ws"], "isNew": r["is_new"], "active": r["active"], "hist": r["code"] in hist,
            "daysLeft": r["days_left"],
        })
    from . import expiry
    return render_template("drugs.html", data={"rows": rows, "ws": wholesalers(), "days": days, "expAlert": expiry.alert_count()})


@bp.post("/api/drug/<code>/delete")
def api_drug_delete(code):
    """약 삭제. 주문·입고 기록이 있으면 기록이 깨지므로 막고 숨기기를 권함."""
    if not db.one("SELECT 1 FROM drugs WHERE code=?", (code,)):
        return err("이미 지워진 약이에요.")
    if db.one("SELECT 1 FROM order_items WHERE code=?", (code,)) or db.one("SELECT 1 FROM receipts WHERE code=?", (code,)):
        return err("주문·입고 기록이 있는 약은 지울 수 없어요. 안 쓰는 약이면 [숨기기]를 눌러주세요.")
    conn = db.connect()
    conn.execute("DELETE FROM cart WHERE code=?", (code,))
    conn.execute("DELETE FROM counts WHERE code=?", (code,))
    conn.execute("DELETE FROM name_links WHERE code=?", (code,))
    conn.execute("UPDATE ledger_lines SET code=NULL WHERE code=?", (code,))
    conn.execute("UPDATE drugs SET alias_of=NULL WHERE alias_of=?", (code,))
    conn.execute("DELETE FROM drugs WHERE code=?", (code,))
    conn.commit()
    return ok()


@bp.post("/api/drugs/bulk_ws")
def api_drugs_bulk_ws():
    """선택한 약들의 자주 담는 도매상을 한 번에 바꿈 (ws 없으면 지정 해제)."""
    j = request.get_json()
    ws = j.get("ws") or None
    if ws and not db.one("SELECT 1 FROM wholesalers WHERE id=?", (ws,)):
        return err("도매상을 찾지 못했어요.")
    codes = [c for c in j.get("codes", []) if c]
    conn = db.connect()
    for c in codes:
        conn.execute("UPDATE drugs SET pref_ws=? WHERE code=?", (ws, c))
    conn.commit()
    return ok(count=len(codes))


EDITABLE = {"order_name", "pack", "pack_label", "target", "pref_ws", "psy", "buy_price", "unit", "active",
            "maker", "insured", "price", "name"}


@bp.post("/api/drug/<code>")
def drug_update(code):
    j = request.get_json()
    if not db.one("SELECT 1 FROM drugs WHERE code=?", (code,)):
        return err("약을 찾지 못했어요.", 404)
    for k, v in j.items():
        if k not in EDITABLE:
            continue
        if k in ("pack",):
            v = int(float(v)) if v not in (None, "") else 0
            db.run("UPDATE drugs SET pack=?, pack_src='manual' WHERE code=?", (v, code))
            g = db.one("SELECT pack_label, unit FROM drugs WHERE code=?", (code,))
            if not g["pack_label"] or g["pack_label"].endswith(("T", "C", "정", "펜")):
                db.run("UPDATE drugs SET pack_label=? WHERE code=?", (f"{v}{'T' if g['unit'] == '정' else g['unit']}", code))
            continue
        if k in ("target", "buy_price", "price"):
            v = float(str(v).replace(",", "")) if v not in (None, "") else None
        if k in ("psy", "active", "insured"):
            v = 1 if v in (1, True, "1", "true") else 0
        if k == "pref_ws":
            v = int(v) if v not in (None, "", "0") else None
        db.run(f"UPDATE drugs SET {k}=? WHERE code=?", (v, code))
        if k == "target" and v is not None:
            calc.set_base_if_missing(code, v)
    g = db.one("SELECT pack, target FROM drugs WHERE code=?", (code,))
    if g["pack"] and g["target"] is not None:
        db.run("UPDATE drugs SET is_new=0 WHERE code=?", (code,))
    return ok()


def record_stock(code, qty, on_date=None):
    """실사·재고 보정: 기록을 남기고 그 수량을 그날(기본 오늘) 진료 마감 기준 재고로 다시 잡음."""
    on_date = on_date or db.today()
    db.run("INSERT INTO counts(code, date, qty, created_at) VALUES (?,?,?,?)", (code, on_date, qty, db.now()))
    calc.set_base(code, qty, on_date)


@bp.post("/api/drug/<code>/stock")
def drug_stock(code):
    """향정 실사 또는 재고 보정: 그 수량을 기준 재고로 다시 잡음."""
    j = request.get_json()
    record_stock(code, float(str(j["qty"]).replace(",", "")))
    return ok()


@bp.post("/api/drugs/stock_bulk")
def drug_stock_bulk():
    """재고 일괄 입력: {date: 기준일(그날 진료 끝난 뒤), items: [{code, qty(정)}]}."""
    j = request.get_json()
    on_date = (j.get("date") or "").strip() or db.today()
    try:
        if date.fromisoformat(on_date) > date.fromisoformat(db.today()):
            return err("기준일은 오늘이나 지난 날짜여야 해요.")
    except ValueError:
        return err("기준일 형식이 맞지 않아요.")
    n = 0
    for it in j.get("items", []):
        try:
            qty = float(it["qty"])
        except (TypeError, ValueError, KeyError):
            continue
        if qty < 0 or not db.one("SELECT 1 FROM drugs WHERE code=?", (it["code"],)):
            continue
        record_stock(it["code"], qty, on_date)
        n += 1
    return ok(count=n)


@bp.post("/api/drugs/apply_suggest")
def apply_suggest():
    days = int(db.get_setting("default_days") or 21)
    n = 0
    for r in calc.drug_rows():
        if r["target"] is None and r["pack"]:
            t = calc.suggest_target(r["daily"], days)
            if t:
                db.run("UPDATE drugs SET target=?, is_new=0 WHERE code=?", (t, r["code"]))
                calc.set_base_if_missing(r["code"], t)
                n += 1
    return ok(count=n)


@bp.post("/api/drugs/bulk_pack")
def bulk_pack():
    """'약 이름 = 숫자 [단위]' 여러 줄을 받아 포장단위를 한 번에 넣음."""
    import re as _re
    text = request.get_json().get("text", "")
    norm = lambda x: _re.sub(r"\s+", "", x or "").lower()
    drugs = db.q("SELECT code, name, order_name FROM drugs WHERE alias_of IS NULL")
    index = {}
    for g in drugs:
        for k in {norm(importers.short_name(g["name"])), norm(g["order_name"]), norm(g["name"])}:
            if k:
                index.setdefault(k, set()).add(g["code"])
    done, missing = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _re.match(r"^(.+?)\s*[=:,\t]\s*(\d+)\s*(정|캡슐|펜|포|병|ml|mL)?\s*$", line) or _re.match(r"^(.+?)\s+(\d+)\s*(정|캡슐|펜|포|병|ml|mL)?\s*$", line)
        if not m:
            missing.append(line)
            continue
        name, num, unit = m.group(1), int(m.group(2)), m.group(3)
        codes = index.get(norm(name), set())
        if len(codes) != 1:
            missing.append(line + ("  (같은 이름이 여러 개)" if codes else ""))
            continue
        code = codes.pop()
        label = f"{num}{'C' if unit == '캡슐' else 'T' if unit in (None, '정') else unit}"
        db.run("UPDATE drugs SET pack=?, pack_label=?, pack_src='manual' WHERE code=?", (num, label, code))
        if unit:
            db.run("UPDATE drugs SET unit=? WHERE code=?", (unit, code))
        g = db.one("SELECT target FROM drugs WHERE code=?", (code,))
        if g["target"] is not None:
            db.run("UPDATE drugs SET is_new=0 WHERE code=?", (code,))
        done.append(name)
    return ok(done=done, missing=missing)


@bp.get("/api/catalog")
def catalog_search():
    qtxt = (request.args.get("q") or "").strip()
    if len(qtxt) < 1:
        return ok(rows=[])
    like = f"%{qtxt}%"
    rows = db.q("""SELECT c.*, (SELECT 1 FROM drugs d WHERE d.code=c.code) AS have FROM catalog c
                   WHERE c.name LIKE ? OR c.maker LIKE ? OR c.code LIKE ? ORDER BY c.used DESC, c.name LIMIT 40""",
                (like, like, like))
    return ok(rows=[{"code": r["code"], "name": r["name"], "maker": r["maker"], "insured": r["insured"],
                     "price": r["price"], "have": bool(r["have"])} for r in rows])


@bp.post("/api/drugs/add")
def drug_add():
    j = request.get_json()
    if j.get("catalog"):
        c = db.one("SELECT * FROM catalog WHERE code=?", (j["catalog"],))
        if not c:
            return err("등록수가에서 약을 찾지 못했어요.")
        if db.one("SELECT 1 FROM drugs WHERE code=?", (c["code"],)):
            db.run("UPDATE drugs SET active=1 WHERE code=?", (c["code"],))
            return ok(code=c["code"], existed=True)
        db.run("""INSERT INTO drugs(code, name, order_name, maker, claim_code, ingr_code, insured, price, is_new, created_at)
                  VALUES (?,?,?,?,?,?,?,?,1,?)""",
               (c["code"], c["name"], importers.short_name(c["name"]), c["maker"], c["claim_code"], c["ingr_code"],
                c["insured"], c["price"] if c["insured"] else 0, db.now()))
        return ok(code=c["code"])
    name = (j.get("name") or "").strip()
    if not name:
        return err("약 이름을 입력해 주세요.")
    code = "M" + datetime.now().strftime("%y%m%d%H%M%S")
    pack = int(j.get("pack") or 0)
    insured = 1 if j.get("insured", 1) in (1, "1", True) else 0
    price = float(j.get("price") or 0)
    db.run("""INSERT INTO drugs(code, name, order_name, maker, insured, price, buy_price, pack, pack_label, pack_src,
              psy, target, pref_ws, is_new, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
           (code, name, j.get("orderName") or name, j.get("maker") or "", insured, price if insured else 0,
            None if insured else (price or None), pack, f"{pack}T" if pack else "", "manual" if pack else "",
            1 if j.get("psy") else 0, float(j["target"]) if j.get("target") else None,
            int(j["pref"]) if j.get("pref") else None, db.now()))
    if j.get("target"):
        calc.set_base_if_missing(code, float(j["target"]))
    return ok(code=code)


# ======================================================================== 업로드
@bp.route("/upload")
def upload():
    imports = db.q("SELECT ui.*, (SELECT COUNT(*) FROM usage WHERE import_id=ui.id) AS n FROM usage_imports ui ORDER BY start DESC")
    last_end = calc.latest_usage_end()
    nxt = None
    if last_end:
        s = date.fromisoformat(last_end) + timedelta(days=1)
        e = date.today() - timedelta(days=1)
        nxt = (s.isoformat(), e.isoformat()) if s <= e else None
    ledgers = db.q("""SELECT li.*, w.name AS ws, (SELECT COUNT(*) FROM ledger_lines WHERE import_id=li.id AND is_disc=0) AS n,
                       (SELECT COUNT(*) FROM ledger_lines WHERE import_id=li.id AND is_disc=0 AND code IS NULL) AS unl
                      FROM ledger_imports li JOIN wholesalers w ON w.id=li.ws_id ORDER BY li.period_start DESC""")
    cat = db.one("SELECT COUNT(*) AS n FROM catalog")["n"]
    unmatched = db.one("""SELECT COUNT(DISTINCT ll.name) AS n FROM ledger_lines ll WHERE ll.code IS NULL AND ll.is_disc=0
                          AND NOT EXISTS (SELECT 1 FROM name_links nl JOIN ledger_imports li ON li.id=ll.import_id
                                          WHERE nl.ws_id=li.ws_id AND nl.ledger_name=ll.name AND nl.code='-')""")["n"]
    logs = db.q("SELECT * FROM import_log ORDER BY id DESC LIMIT 15")
    watch = {k: db.get_setting(k) for k in ("watch_dir", "watch_mode", "watch_day", "watch_time", "watch_last")}
    return render_template("upload.html", imports=imports, nxt=nxt, ledgers=ledgers, ws=wholesalers(),
                           catalog_n=cat, unmatched=unmatched, logs=logs, watch=watch, weekdays=autoimport.WEEKDAYS)


@bp.post("/upload/auto")
def upload_auto():
    """파일 종류를 알아서 판단: 제약사별 발생 통계 / 사용자등록수가 / 매출원장(PDF·엑셀)."""
    files = [f for f in request.files.getlist("file") if f and f.filename]
    if not files:
        flash("파일을 골라주세요.", "error")
        return redirect(url_for("main.upload"))
    ws_id = int(request.form.get("ws") or 0) or None
    results = autoimport.process([(f.filename, f.read()) for f in files], ws_id, bool(request.form.get("replace")))
    autoimport.log(results, "직접 올림")
    _flash_results(results)
    return redirect(url_for("main.upload"))


@bp.post("/upload/stats")
def upload_stats():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("파일을 골라주세요.", "error")
        return redirect(url_for("main.upload"))
    start, end = request.form.get("start"), request.form.get("end")
    if not start or not end:
        s2, e2 = importers.parse_period(f.filename)
        start, end = start or s2, end or e2
    try:
        r = importers.import_stats(f.read(), f.filename, start, end, replace=bool(request.form.get("replace")))
        flash(f"{start} ~ {end} 사용량 {r['count']}품목을 반영했어요." + (f" 새 약 {r['added']}개가 '신규'로 추가됐어요." if r["added"] else ""), "ok")
    except Exception as e:
        flash(str(e), "error")
    return redirect(url_for("main.upload"))


def _flash_results(results):
    for name, cat, msg in results:
        flash(f"{name}: {msg}", cat)


@bp.post("/upload/master")
@bp.post("/upload/ledger")
def upload_files():
    """예전 칸(등록수가·매출원장)도 자동 인식 처리를 그대로 씀. 매출원장은 고른 도매상으로."""
    files = [f for f in request.files.getlist("file") if f and f.filename]
    if not files:
        flash("파일을 골라주세요.", "error")
        return redirect(url_for("main.upload"))
    results = autoimport.process([(f.filename, f.read()) for f in files], int(request.form.get("ws") or 0) or None)
    autoimport.log(results, "직접 올림")
    _flash_results(results)
    return redirect(url_for("main.upload"))


@bp.route("/links")
def links():
    rows = db.q("""SELECT li.ws_id, w.name AS ws, ll.name, ll.spec, MAX(ll.date) AS last, COUNT(*) AS n
                   FROM ledger_lines ll JOIN ledger_imports li ON li.id=ll.import_id JOIN wholesalers w ON w.id=li.ws_id
                   WHERE ll.code IS NULL AND ll.is_disc=0
                     AND NOT EXISTS (SELECT 1 FROM name_links nl WHERE nl.ws_id=li.ws_id AND nl.ledger_name=ll.name AND nl.code='-')
                   GROUP BY li.ws_id, ll.name ORDER BY w.name, ll.name""")
    drugs = [{"code": r["code"], "name": r["name"], "maker": r["maker"]} for r in db.q("SELECT * FROM drugs ORDER BY name")]
    return render_template("links.html", rows=rows, drugs=drugs)


@bp.post("/api/link")
def link_save():
    j = request.get_json()
    ws_id, name, code = int(j["ws"]), j["name"], j["code"]
    db.run("INSERT OR REPLACE INTO name_links(ws_id, ledger_name, code) VALUES (?,?,?)", (ws_id, name, code))
    importers.link_ledger_names(ws_id)
    return ok()


# ======================================================================== 리포트
def report_unlocked():
    t = session.get("report_ok")
    if t and time.time() - t < REPORT_TIMEOUT:
        session["report_ok"] = time.time()
        return True
    return False


def month_range(m):
    y, mo = int(m[:4]), int(m[5:7])
    s = date(y, mo, 1)
    e = date(y + (mo == 12), 1 if mo == 12 else mo + 1, 1) - timedelta(days=1)
    return s, e


def unit_prices():
    """약별 1정(단위) 단가와 근거. 매출원장 단가가 있으면 그것(최근 거래), 없으면 급여=약가, 비급여=통당 매입가/포장."""
    roots = calc.root_map()
    led = {}
    for r in db.q("""SELECT code, price, spec, date FROM ledger_lines
                     WHERE code IS NOT NULL AND code != '-' AND is_disc=0 AND price > 0 ORDER BY date"""):
        units = importers.pack_from_spec(r["spec"]) or 0
        if units:
            led[roots.get(r["code"], r["code"])] = r["price"] / units
    out = {}
    for g in db.q("SELECT code, insured, price, buy_price, pack, alias_of FROM drugs"):
        root = g["alias_of"] or g["code"]
        if root in led:
            out[g["code"]] = (led[root], "원장")
        elif g["insured"] and g["price"]:
            out[g["code"]] = (g["price"], "약가")
        elif g["buy_price"] and g["pack"]:
            out[g["code"]] = (g["buy_price"] / g["pack"], "매입가")
        else:
            out[g["code"]] = (None, "단가 없음")
    return out


def price_of(up, code):
    """unit_prices() 결과에서 1정 단가(없으면 0)."""
    return up.get(code, (None,))[0] or 0


def report_data(month):
    s, e = month_range(month)
    S, E = s.isoformat(), e.isoformat()
    wss = {w["id"]: w["name"] for w in wholesalers(False)}
    up = unit_prices()
    names = {r["code"]: (r["oname"], r["maker"], r["insured"]) for r in db.q(
        "SELECT code, COALESCE(NULLIF(order_name,''), name) AS oname, maker, insured FROM drugs")}

    # 입고 기준 구매액
    by_ws, by_maker, by_drug, missing = {}, {}, {}, set()
    total = ins_total = non_total = 0.0
    for r in db.q("SELECT * FROM receipts WHERE date BETWEEN ? AND ?", (S, E)):
        units = r["packs"] * r["pack"]
        price, src = up.get(r["code"], (None, "단가 없음"))
        nm, maker, insured = names.get(r["code"], (r["code"], "", 1))
        amt = price * units if price is not None else 0
        if price is None:
            missing.add(nm)
        total += amt
        if insured:
            ins_total += amt
        else:
            non_total += amt
        wn = wss.get(r["ws_id"], "도매상 미지정")
        by_ws[wn] = by_ws.get(wn, 0) + amt
        by_maker[maker or "기타"] = by_maker.get(maker or "기타", 0) + amt
        d = by_drug.setdefault(r["code"], {"name": nm, "maker": maker or "", "ws": set(), "packs": 0, "units": 0,
                                           "amount": 0, "unit": price, "src": src, "insured": insured})
        d["ws"].add(wn)
        d["packs"] += r["packs"]
        d["units"] += units
        d["amount"] += amt
    drugs = sorted(({**d, "ws": ", ".join(sorted(d["ws"]))} for d in by_drug.values()), key=lambda x: -x["amount"])

    # 주문 기준 금액 (이 달에 확정한 주문)
    ordered, ordered_ws = 0.0, {}
    orders_n = {}
    for r in db.q("""SELECT o.ws_id, oi.code, oi.packs, oi.pack FROM order_items oi JOIN orders o ON o.id=oi.order_id
                     WHERE o.order_date BETWEEN ? AND ?""", (S, E)):
        amt = price_of(up, r["code"]) * r["packs"] * r["pack"]
        ordered += amt
        wn = wss.get(r["ws_id"], "?")
        ordered_ws[wn] = ordered_ws.get(wn, 0) + amt
    for r in db.q("SELECT ws_id, COUNT(*) AS n FROM orders WHERE order_date BETWEEN ? AND ? GROUP BY ws_id", (S, E)):
        orders_n[wss.get(r["ws_id"], "?")] = r["n"]

    # 아직 안 들어온 금액 (지금 기준, 미입고 + 미송)
    pending = 0.0
    for r in db.q(f"SELECT oi.code, oi.pack, {_left_sql()} AS left FROM order_items oi"):
        if r["left"] > 0:
            pending += price_of(up, r["code"]) * r["left"] * r["pack"]

    ledger = {}
    for r in db.q("""SELECT li.ws_id, SUM(CASE WHEN ll.is_disc=0 THEN ll.amount ELSE 0 END) AS gross,
                            SUM(CASE WHEN ll.is_disc=1 THEN ll.amount ELSE 0 END) AS disc, COUNT(DISTINCT ll.date) AS days
                     FROM ledger_lines ll JOIN ledger_imports li ON li.id=ll.import_id
                     WHERE ll.date BETWEEN ? AND ? GROUP BY li.ws_id""", (S, E)):
        ledger[wss.get(r["ws_id"], "?")] = {"gross": r["gross"] or 0, "disc": r["disc"] or 0,
                                           "net": (r["gross"] or 0) + (r["disc"] or 0), "days": r["days"]}
    usage_amt, usage_cost, umakers, covered = 0.0, 0.0, {}, set()
    for r in db.q("""SELECT ui.start, ui.end, u.code, u.qty, u.amount, d.maker FROM usage u JOIN usage_imports ui ON ui.id=u.import_id
                     LEFT JOIN drugs d ON d.code=u.code WHERE NOT (ui.end < ? OR ui.start > ?)""", (S, E)):
        a, b = date.fromisoformat(r["start"]), date.fromisoformat(r["end"])
        lo, hi = max(a, s), min(b, e)
        ov = (hi - lo).days + 1
        ratio = ov / ((b - a).days + 1)
        part = (r["amount"] or 0) * ratio
        usage_amt += part
        usage_cost += (r["qty"] or 0) * ratio * price_of(up, r["code"])
        umakers[r["maker"] or "기타"] = umakers.get(r["maker"] or "기타", 0) + part
        d0 = lo
        while d0 <= hi:
            covered.add(d0)
            d0 += timedelta(days=1)
    month_days = (e - s).days + 1
    # 이 달 총 구매 금액: 매출원장이 있는 도매상은 원장 실제(할인 반영), 없으면 입고 기준
    purchase_total, purchase_parts = 0.0, []
    for name in sorted(set(by_ws) | set(ledger)):
        if name in ledger:
            v, how = ledger[name]["net"], "원장"
        else:
            v, how = by_ws.get(name, 0), "입고 기준"
        purchase_total += v
        purchase_parts.append({"ws": name, "amount": v, "how": how})
    ws_table = []
    for name in sorted(set(by_ws) | set(ledger) | set(orders_n) | set(ordered_ws)):
        ws_table.append({"ws": name, "orders": orders_n.get(name, 0), "ordered": ordered_ws.get(name, 0),
                         "est": by_ws.get(name, 0), "ledger": ledger.get(name)})
    active_ws = [w["name"] for w in wholesalers()]
    missing_ws = [n for n in active_ws if n not in {p["ws"] for p in purchase_parts}]
    return {"purchase": purchase_total, "purchaseParts": purchase_parts, "missingWs": missing_ws, "usageCost": usage_cost,
            "covered": len(covered), "monthDays": month_days,
            "total": total, "insured": ins_total, "noninsured": non_total, "ordered": ordered, "pending": pending,
            "byWs": by_ws, "byMaker": sorted(by_maker.items(), key=lambda x: -x[1])[:10],
            "ledger": ledger, "usage": usage_amt, "usageMakers": sorted(umakers.items(), key=lambda x: -x[1])[:8],
            "wsTable": ws_table, "missing": sorted(missing), "drugs": drugs}


def _month_day(y, m, d):
    import calendar
    return date(y, m, min(d, calendar.monthrange(y, m)[1]))


def billing_periods(billing_day, today=None):
    """(지난 마감 기간, 이번 진행 중 기간). 마감일이 없으면 달력 월 단위."""
    today = today or date.today()
    def end_on_or_after(dt):
        if billing_day is None:
            nxt = (dt.replace(day=28) + timedelta(days=4)).replace(day=1)
            return nxt - timedelta(days=1)
        e = _month_day(dt.year, dt.month, billing_day)
        if e < dt:
            nm = (dt.replace(day=28) + timedelta(days=4)).replace(day=1)
            e = _month_day(nm.year, nm.month, billing_day)
        return e
    cur_end = end_on_or_after(today)
    prev_end = end_on_or_after((cur_end.replace(day=1) - timedelta(days=1)).replace(day=1))
    if prev_end >= cur_end:
        prev_end = end_on_or_after(prev_end.replace(day=1) - timedelta(days=40))
    prev_prev_end = end_on_or_after((prev_end.replace(day=1) - timedelta(days=1)).replace(day=1))
    return (prev_prev_end + timedelta(days=1), prev_end), (prev_end + timedelta(days=1), cur_end)


def billing_amounts(ws_id, start, end):
    S, E = start.isoformat(), end.isoformat()
    up = unit_prices()
    rec = 0.0
    for r in db.q("SELECT code, packs, pack FROM receipts WHERE ws_id=? AND date BETWEEN ? AND ?", (ws_id, S, E)):
        rec += price_of(up, r["code"]) * r["packs"] * r["pack"]
    led = db.one("""SELECT SUM(CASE WHEN ll.is_disc=0 THEN ll.amount ELSE 0 END) AS gross, COUNT(*) AS n
                    FROM ledger_lines ll JOIN ledger_imports li ON li.id=ll.import_id
                    WHERE li.ws_id=? AND ll.date BETWEEN ? AND ?""", (ws_id, S, E))
    covered = db.one("SELECT MAX(period_end) AS e FROM ledger_imports WHERE ws_id=?", (ws_id,))["e"]
    has_led = bool(led["n"])
    full = bool(covered and covered >= E)
    after = 0.0  # 원장이 끝난 날 이후의 입고분 (원장이 기간 일부만 덮을 때)
    if has_led and not full:
        for r in db.q("SELECT code, packs, pack FROM receipts WHERE ws_id=? AND date > ? AND date <= ?", (ws_id, covered, E)):
            after += price_of(up, r["code"]) * r["packs"] * r["pack"]
    base = ((led["gross"] or 0) + after) if has_led else rec
    return {"start": S, "end": E, "receipts": rec, "after": after, "covered": covered, "base": base,
            "ledger": (led["gross"] or 0) if has_led else None, "ledgerFull": full}


def billing_data(custom=None):
    out = []
    for w in wholesalers():
        prev, cur = billing_periods(w["billing_day"])
        rows = [("지난 마감분", *prev), ("이번 기간 (진행 중)", *cur)]
        if custom:
            rows.append(("직접 지정", *custom))
        out.append({"id": w["id"], "name": w["name"], "day": w["billing_day"],
                    "rate": w["discount_rate"] or 0, "on": bool(w["discount_on"]),
                    "periods": [{"label": lb, **billing_amounts(w["id"], a, b)} for lb, a, b in rows]})
    return out


@bp.post("/api/ws/<int:wid>/discount")
def ws_discount(wid):
    if not report_unlocked():
        return err("리포트 잠금을 먼저 풀어주세요.", 403)
    j = request.get_json()
    db.run("UPDATE wholesalers SET discount_on=? WHERE id=?", (1 if j.get("on") else 0, wid))
    if "rate" in j:
        db.run("UPDATE wholesalers SET discount_rate=? WHERE id=?", (float(j["rate"] or 0), wid))
    return ok()


@bp.route("/report", methods=["GET", "POST"])
def report():
    has_pw = bool(db.get_setting("report_pw"))
    if request.method == "POST":
        pw = request.form.get("pw", "")
        if not has_pw:
            if len(pw) < 4 or pw != request.form.get("pw2"):
                flash("비밀번호는 4자 이상, 두 번 똑같이 입력해 주세요.", "error")
                return redirect(url_for("main.report"))
            db.set_setting("report_pw", generate_password_hash(pw))
            session["report_ok"] = time.time()
        elif check_password_hash(db.get_setting("report_pw"), pw):
            session["report_ok"] = time.time()
        else:
            flash("비밀번호가 맞지 않아요.", "error")
        return redirect(url_for("main.report", m=request.args.get("m")))
    if not report_unlocked():
        return render_template("report_lock.html", has_pw=has_pw)
    month = request.args.get("m") or date.today().strftime("%Y-%m")
    s, _ = month_range(month)
    prev = (s - timedelta(days=1)).strftime("%Y-%m")
    nxt = (s + timedelta(days=32)).strftime("%Y-%m")
    trend = []
    cur = s
    for _ in range(6):
        mm = cur.strftime("%Y-%m")
        rd = report_data(mm)
        trend.append({"m": mm, "est": rd["total"], "ledger": sum(v["net"] for v in rd["ledger"].values())})
        cur = (cur - timedelta(days=1)).replace(day=1)
    trend.reverse()
    custom = None
    ps, pe = request.args.get("ps"), request.args.get("pe")
    if ps and pe and ps <= pe:
        custom = (date.fromisoformat(ps), date.fromisoformat(pe))
    from . import leave
    return render_template("report.html", m=month, prev=prev, nxt=nxt, r=report_data(month), trend=trend,
                           billing=billing_data(custom), ps=ps or "", pe=pe or "", leave=leave.admin_data())


@bp.route("/report/lock")
def report_lock():
    session.pop("report_ok", None)
    return redirect(url_for("main.report"))


@bp.route("/report.xlsx")
def report_xlsx():
    if not report_unlocked():
        return redirect(url_for("main.report"))
    import openpyxl
    month = request.args.get("m") or date.today().strftime("%Y-%m")
    r = report_data(month)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{month} 요약"
    ws.append(["항목", "금액(원)"])
    ws.append(["이 달 총 구매 금액(원장 있으면 원장, 없으면 입고 기준)", round(r["purchase"])])
    ws.append(["이 달 처방된 약 금액(eghis 통계 금액)", round(r["usage"])])
    ws.append(["  처방량을 구매 단가로 환산", round(r["usageCost"])])
    ws.append(["이 달 주문액(주문 확정 기준)", round(r["ordered"])])
    ws.append(["구매액(입고 기준)", round(r["total"])])
    ws.append(["  급여", round(r["insured"])])
    ws.append(["  비급여", round(r["noninsured"])])
    ws.append(["아직 안 들어온 금액(현재)", round(r["pending"])])
    for name, v in r["ledger"].items():
        ws.append([f"{name} 매출원장 실제(할인 반영)", round(v["net"])])
        ws.append([f"{name} 할인", round(v["disc"])])
    ws.append(["원내 사용액(통계 금액)", round(r["usage"])])
    w2 = wb.create_sheet("도매상별")
    w2.append(["도매상", "주문 횟수", "주문액", "구매액(입고)", "원장 실제"])
    for t in r["wsTable"]:
        w2.append([t["ws"], t["orders"], round(t["ordered"]), round(t["est"]), round(t["ledger"]["net"]) if t["ledger"] else None])
    w4 = wb.create_sheet("약별")
    w4.append(["약품", "제약사", "도매상", "입고 통", "입고 정", "1정 단가", "단가 근거", "금액"])
    for d in r["drugs"]:
        w4.append([d["name"], d["maker"], d["ws"], d["packs"], d["units"], round(d["unit"], 1) if d["unit"] else None, d["src"], round(d["amount"])])
    w3 = wb.create_sheet("제약사별")
    w3.append(["제약사", "구매액 추정"])
    for k, v in r["byMaker"]:
        w3.append([k, round(v)])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, download_name=f"월간리포트_{month}.xlsx", as_attachment=True)


# ======================================================================== 설정
def addresses():
    host = socket.gethostname()
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips = [i for i in ips if not i.startswith("127.")]
    port = os.environ.get("DRUG_ORDER_PORT", "8000")
    return [f"http://{host}:{port}"] + [f"http://{i}:{port}" for i in sorted(ips)]


def qr_svg(text):
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


@bp.route("/settings")
def settings():
    wss = wholesalers()
    counts = {r["pref_ws"]: r["n"] for r in db.q("SELECT pref_ws, COUNT(*) AS n FROM drugs WHERE active=1 GROUP BY pref_ws")}
    addrs = addresses()
    return render_template("settings.html", ws=wss, counts=counts, staff=db.q("SELECT * FROM staff WHERE active=1"),
                           s={k: db.get_setting(k) for k in db.DEFAULTS}, backups=maint.list_backups()[:10],
                           addrs=addrs, qr=qr_svg(addrs[1] if len(addrs) > 1 else addrs[0]), version=maint.version())


def _roots():
    """폴더 고르기 시작점: 드라이브(윈도우) + 자주 쓰는 폴더."""
    import string
    out = []
    home = os.path.expanduser("~")
    for label, sub in (("바탕화면", "Desktop"), ("문서", "Documents"), ("다운로드", "Downloads")):
        p = os.path.join(home, sub)
        if os.path.isdir(p):
            out.append({"label": label, "path": p})
    if os.name == "nt":
        for d in string.ascii_uppercase:
            p = f"{d}:\\"
            if os.path.exists(p):
                out.append({"label": f"{d}: 드라이브" + (" (구글 드라이브일 수 있어요)" if d in "GH" else ""), "path": p})
    else:
        out.append({"label": "/", "path": "/"})
    out.append({"label": "홈 폴더", "path": home})
    return out


@bp.get("/api/dirs")
def list_dirs():
    path = (request.args.get("path") or "").strip()
    if not path:
        return ok(path="", parent=None, dirs=[], roots=_roots())
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return err("폴더를 찾지 못했어요.")
    try:
        names = sorted((n for n in os.listdir(path) if not n.startswith((".", "$")) and os.path.isdir(os.path.join(path, n))),
                       key=str.lower)
    except OSError as e:
        return err(f"이 폴더는 열 수 없어요. ({e.strerror})")
    norm = os.path.normpath(path)
    parent = os.path.dirname(norm)
    parent = None if parent == norm else parent  # 드라이브 맨 위(C:\)나 / 에서는 위 폴더 없음
    return ok(path=path, parent=parent or "", dirs=names[:500], roots=_roots())


@bp.post("/api/dirs/mkdir")
def make_dir():
    j = request.get_json()
    name = (j.get("name") or "").strip()
    if not name or any(ch in name for ch in '\\/:*?"<>|'):
        return err("폴더 이름에 쓸 수 없는 글자가 있어요.")
    p = os.path.join(j.get("path") or "", name)
    try:
        os.makedirs(p, exist_ok=True)
    except OSError as e:
        return err(f"폴더를 만들지 못했어요. ({e.strerror})")
    return ok(path=p)


@bp.post("/settings/<action>")
def settings_post(action):
    f = request.form
    try:
        if action == "ws_add":
            name = f.get("name", "").strip()
            if name:
                db.run("INSERT INTO wholesalers(name, sort) VALUES (?, (SELECT COALESCE(MAX(sort),0)+1 FROM wholesalers))", (name,))
                flash(f"'{name}' 도매상을 추가했어요.", "ok")
        elif action == "ws_edit":
            bd = f.get("billing_day", "").strip()
            bd = min(31, max(1, int(bd))) if bd.isdigit() else None
            rate = float(f.get("discount_rate") or 0)
            db.run("""UPDATE wholesalers SET name=?, header=?, line_fmt=?, footer=?, billing_day=?, discount_rate=?, discount_on=?
                      WHERE id=?""",
                   (f["name"].strip(), f.get("header") or None, f.get("line_fmt") or None, f.get("footer") or None,
                    bd, rate, 1 if f.get("discount_on") else 0, int(f["id"])))
            flash("도매상 정보를 저장했어요.", "ok")
        elif action == "ws_delete":
            wid = int(f["id"])
            db.run("UPDATE wholesalers SET active=0 WHERE id=?", (wid,))
            db.run("UPDATE drugs SET pref_ws=NULL WHERE pref_ws=?", (wid,))
            db.run("DELETE FROM cart WHERE ws_id=?", (wid,))
            flash("도매상을 삭제했어요. 지난 주문·입고 기록은 그대로 남아요.", "ok")
        elif action == "msg":
            for k in ("msg_header", "msg_line", "msg_footer"):
                db.set_setting(k, f.get(k, "").replace("\r\n", "\n"))
            flash("카톡 문구 틀을 저장했어요.", "ok")
        elif action == "general":
            db.set_setting("clinic_name", f.get("clinic_name", "").strip())
            db.set_setting("default_days", str(int(f.get("default_days") or 21)))
            flash("저장했어요.", "ok")
        elif action == "staff_add":
            n = f.get("name", "").strip()
            if n:
                db.run("INSERT INTO staff(name) VALUES (?)", (n,))
        elif action == "staff_delete":
            db.run("UPDATE staff SET active=0 WHERE id=?", (int(f["id"]),))
        elif action == "pw":
            cur = db.get_setting("report_pw")
            if cur and not check_password_hash(cur, f.get("cur", "")):
                flash("현재 비밀번호가 맞지 않아요.", "error")
            elif len(f.get("new", "")) < 4:
                flash("새 비밀번호는 4자 이상으로 해주세요.", "error")
            else:
                db.set_setting("report_pw", generate_password_hash(f["new"]))
                flash("리포트 비밀번호를 바꿨어요.", "ok")
        elif action == "psy_mode":
            cur = db.get_setting("report_pw")
            new = "excel" if f.get("psy_mode") == "excel" else "manual"
            if not cur:
                flash("향정 재고 방식을 바꾸려면 먼저 아래에서 리포트 비밀번호(원장 비밀번호)를 정해 주세요.", "error")
            elif not check_password_hash(cur, f.get("pw", "")):
                flash("원장 비밀번호가 맞지 않아요. 향정 재고 방식은 그대로예요.", "error")
            elif new == db.get_setting("psy_mode"):
                flash("이미 그 방식이에요.", "ok")
            else:
                db.set_setting("psy_mode", new)
                flash("향정 재고를 이제 " + ("향정 관리에 적은 사용량(수기)" if new == "manual" else "처방 통계 엑셀") + "으로 계산해요.", "ok")
            return redirect(url_for("main.settings") + "#psymode")
        elif action == "backup_dir":
            db.set_setting("backup_dir", f.get("backup_dir", "").strip())
            flash("백업 폴더를 저장했어요.", "ok")
        elif action == "backup_now":
            r = maint.backup_now("manual")
            flash(r["error"] or ("백업했어요." + (" 구글 드라이브 폴더에도 저장했어요." if r["drive"] else " (구글 드라이브 폴더가 설정되지 않아 이 PC에만 저장)")),
                  "error" if r["error"] else "ok")
        elif action == "restore":
            up = request.files.get("file")
            if up and up.filename:
                maint.restore_from(up.read())
            else:
                path = f.get("path")
                if path not in [b["path"] for b in maint.list_backups()]:
                    raise ValueError("백업 파일을 골라주세요.")
                maint.restore_from(path)
            flash("백업에서 복원했어요. (복원 직전 상태도 따로 백업해 두었어요.)", "ok")
        elif action == "watch":
            folder = f.get("watch_dir", "").strip()
            if folder and not os.path.isdir(folder):
                os.makedirs(folder, exist_ok=True)
            db.set_setting("watch_dir", folder)
            db.set_setting("watch_mode", "weekly" if f.get("watch_mode") == "weekly" else "always")
            db.set_setting("watch_day", str(int(f.get("watch_day") or 0)))
            db.set_setting("watch_time", f.get("watch_time") or "09:00")
            flash("자동 읽기 설정을 저장했어요." + (f" 이 폴더에 파일을 저장하면 돼요: {folder}" if folder else " (폴더를 비워서 자동 읽기는 꺼졌어요)"), "ok")
            return redirect(url_for("main.upload") + "#watch")
        elif action == "watch_now":
            res = autoimport.scan(force=True)
            if not res:
                flash("폴더에 새 파일이 없어요." if db.get_setting("watch_dir") else "자동 읽기 폴더를 먼저 정해주세요.", "ok")
            _flash_results(res)
            return redirect(url_for("main.upload") + "#watch")
        elif action == "update":
            changed, out = maint.update()
            if changed:
                flash("업데이트했어요. 잠시 후 자동으로 다시 시작돼요. 10초 뒤 새로고침하세요.", "ok")
                restart_later()
            else:
                flash(out, "error" if "실패" in out or "찾지" in out else "ok")
        elif action == "auto_update":
            on = bool(f.get("on"))
            db.set_setting("auto_update", "1" if on else "0")
            flash("자동 업데이트를 켰어요. 밤에 새 버전이 있으면 알아서 받아요." if on else "자동 업데이트를 껐어요. [업데이트]를 눌러야 새 버전을 받아요.", "ok")
            return redirect(url_for("main.settings") + "#update")
        elif action == "rollback":
            changed, out = maint.rollback()
            flash(out, "ok" if changed else "error")
            if changed:
                restart_later()
    except Exception as e:
        flash(str(e), "error")
    return redirect(url_for("main.settings") + "#" + action)


def restart_later():
    def go():
        time.sleep(1.5)
        os._exit(3)  # start.bat 이 3번 코드를 보면 다시 실행
    threading.Thread(target=go, daemon=True).start()
