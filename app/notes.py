"""인수인계·공지판: 모든 화면 맨 위 띠. 탭 없이 base.html에서 펼쳐 씀."""
from datetime import date, timedelta

from flask import request

from . import db
from .views import bp, ok, err, staff_list


def notes_data():
    today = date.today().isoformat()
    rows = [dict(r) for r in db.q(
        "SELECT * FROM notes WHERE done_at IS NULL OR done_at >= ? ORDER BY pinned DESC, COALESCE(day, substr(created_at,1,10)), id",
        ((date.today() - timedelta(days=7)).isoformat(),))]
    now = [r for r in rows if not r["done_at"] and (not r["day"] or r["day"] == today)]
    soon = [r for r in rows if not r["done_at"] and r["day"] and r["day"] > today]
    old = [r for r in rows if r["done_at"] or (r["day"] and r["day"] < today)]  # 완료했거나 날짜 지난 것
    old.sort(key=lambda r: r["done_at"] or r["day"], reverse=True)
    return {"now": now, "soon": soon, "old": old[:15], "staff": staff_list(), "today": today}


@bp.app_context_processor
def _notes_ctx():
    return {"notes": notes_data()}


@bp.post("/api/notes")
def api_note_add():
    j = request.get_json() or {}
    body = (j.get("body") or "").strip()
    if not body:
        return err("내용을 적어주세요.")
    day = (j.get("day") or "").strip() or None
    if day:
        try:
            date.fromisoformat(day)
        except ValueError:
            return err("날짜가 이상해요.")
    db.run("INSERT INTO notes(body, who, day, pinned, created_at) VALUES (?,?,?,?,?)",
           (body[:500], (j.get("who") or "").strip() or None, day, 1 if j.get("pinned") else 0, db.now()))
    return ok()


@bp.post("/api/notes/<int:nid>")
def api_note_set(nid):
    """act: done | undo | pin | unpin | delete"""
    act = (request.get_json() or {}).get("act")
    sql = {"done": "UPDATE notes SET done_at=? WHERE id=?", "undo": "UPDATE notes SET done_at=NULL, day=CASE WHEN day < ? THEN NULL ELSE day END WHERE id=?",
           "pin": "UPDATE notes SET pinned=1 WHERE id=? AND ?", "unpin": "UPDATE notes SET pinned=0 WHERE id=? AND ?", "delete": "DELETE FROM notes WHERE id=? AND ?"}.get(act)
    if not sql:
        return err("잘못된 요청이에요.")
    args = (db.now(), nid) if act == "done" else (date.today().isoformat(), nid) if act == "undo" else (nid, 1)
    db.run(sql, args)
    return ok()
