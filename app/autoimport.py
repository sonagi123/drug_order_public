"""파일 종류 자동 인식 처리 + 지정 폴더 자동 읽기."""
import json
import os
import shutil
import time
from datetime import date, datetime, timedelta

from . import calc, db, importers

ORDER = ["master", "stats", "ledger-pdf", "ledger-excel"]
EXTS = (".xls", ".xlsx", ".htm", ".html", ".pdf")
MADE = {}  # 자동 읽기 폴더의 파일 이름 → 파일 수정 시각(= eghis에서 내려받은 시각)
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def process(files, ws_id=None, replace=False):
    """files: [(이름, bytes)]. 결과: [(이름, 'ok'|'error', 메시지)]"""
    out, loaded = [], []
    for name, data in files:
        try:
            kind, rows = importers.detect_kind(data, name)
        except Exception as e:
            out.append((name, "error", f"파일을 읽지 못했어요. ({e})"))
            continue
        loaded.append((ORDER.index(kind), name, data, kind, rows))
    # 등록수가 → 통계 → 원장 순서로 처리해야 약 정보가 먼저 들어감
    for _, name, data, kind, rows in sorted(loaded, key=lambda x: x[0]):
        try:
            if kind == "master":
                r = importers.import_master(data, rows=rows)
                out.append((name, "ok", f"사용자등록수가로 인식 — {r['count']}품목 반영"))
            elif kind == "stats":
                start, end = importers.parse_period(name)
                how, rep = "파일 이름의 날짜", replace
                if start and end:
                    rep = True  # 같은 기간을 다시 내보낸 파일이면 새 파일로 바꿈
                    today = date.today().isoformat()
                    if start > today:
                        raise ValueError(f"파일 이름의 시작 날짜({start})가 아직 오지 않았어요.")
                    if end > today:  # 예: 이번 달 말일까지로 뽑은 파일 → 오늘까지만
                        end, how = today, "파일 이름의 날짜, 끝은 오늘까지"
                else:
                    last = calc.latest_usage_end()
                    if not last:
                        raise ValueError("파일 이름에 기간(예: 20261001-20261031)이 없어요. 처음 한 번은 업로드 화면의 '제약사별 발생 통계' 칸에서 기간을 넣어 올려주세요.")
                    start = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
                    end = (date.today() - timedelta(days=1)).isoformat()
                    if start > end:
                        raise ValueError("새로 반영할 기간이 없어요(이미 어제까지 올라와 있어요).")
                    how = "지난번 업로드 다음 날 ~ 어제"
                r = importers.import_stats(data, name, start, end, replace=rep, rows=rows, made_at=MADE.pop(name, None))
                out.append((name, "ok", f"처방 통계로 인식 — {start} ~ {end} ({how}), {r['count']}품목 반영"
                            + (f", 새 약 {r['added']}개" if r["added"] else "")))
            else:
                r = importers.import_ledger(data, name, ws_id, rows=rows)
                out.append((name, "ok", f"{r['ws']} 매출원장으로 인식{' (사업자번호로 자동)' if r['auto'] else ''} — {r['period']}, 명세 {r['days']}일"
                            + (f", 같은 기간 예전 줄 {r['replaced']}개는 새 파일로 바꿈" if r["replaced"] else "")
                            + (f", 약 연결 필요 {len(r['unmatched'])}개" if r["unmatched"] else "")))
        except Exception as e:
            out.append((name, "error", str(e)))
    return out


def log(results, source):
    for name, cat, msg in results:
        db.run("INSERT INTO import_log(at, source, filename, ok, message) VALUES (?,?,?,?,?)",
               (db.now(), source, name, 1 if cat == "ok" else 0, msg))


def due(now=None):
    """설정에 따라 지금 폴더를 확인할 때인지. 정해진 때 PC가 꺼져 있었으면 다음에 켜졌을 때 한 번 실행."""
    now = now or datetime.now()
    if (db.get_setting("watch_mode") or "always") == "always":
        return True
    day = int(db.get_setting("watch_day") or 0)
    hh, mm = (db.get_setting("watch_time") or "09:00").split(":")
    slot = (now - timedelta(days=(now.weekday() - day) % 7)).replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=7)  # 가장 최근에 지나간 예정 시각
    last = db.get_setting("watch_last")
    return not last or last < slot.strftime("%Y-%m-%d %H:%M")


def scan(force=False):
    """지정 폴더의 새 파일을 읽고, 읽은 파일은 '읽음' 폴더로, 실패한 파일은 '실패' 폴더로 옮김."""
    folder = (db.get_setting("watch_dir") or "").strip()
    if not folder:
        return []
    if not os.path.isdir(folder):
        return [("(폴더)", "error", f"자동 읽기 폴더를 찾지 못했어요: {folder}")]
    if not force and not due():
        return []
    db.set_setting("watch_last", datetime.now().strftime("%Y-%m-%d %H:%M"))
    try:
        seen = set(json.loads(db.get_setting("watch_seen") or "[]"))
    except ValueError:
        seen = set()
    files, sigs = [], {}
    for n in sorted(os.listdir(folder)):
        p = os.path.join(folder, n)
        if not os.path.isfile(p) or not n.lower().endswith(EXTS) or n.startswith(("~$", ".")):
            continue
        mt = os.path.getmtime(p)
        if time.time() - mt < 20:  # 아직 저장 중일 수 있음
            continue
        MADE[n] = datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S")  # 처방 반영 시각 표시용
        sig = f"{n}|{int(mt)}|{os.path.getsize(p)}"
        if sig in seen:  # 이미 읽었는데 엑셀에서 열려 있어 못 옮긴 파일
            continue
        try:
            with open(p, "rb") as f:
                files.append((n, f.read()))
            sigs[n] = sig
        except OSError:
            continue  # 다른 프로그램이 잠가 둠 → 다음에
    if not files:
        return []
    results = process(files)
    for name, cat, _ in results:
        seen.add(sigs.get(name, ""))
        dest = os.path.join(folder, "읽음" if cat == "ok" else "실패")
        src = os.path.join(folder, name)
        try:
            os.makedirs(dest, exist_ok=True)
            if os.path.exists(src):
                target = os.path.join(dest, name)
                if os.path.exists(target):
                    base, ext = os.path.splitext(name)
                    target = os.path.join(dest, f"{base}_{datetime.now():%Y%m%d%H%M%S}{ext}")
                shutil.copy2(src, target)
                os.remove(src)
        except OSError:
            pass  # 엑셀에서 열려 있으면 원본은 남지만, 이미 읽었으니 다시 읽지 않음
    db.set_setting("watch_seen", json.dumps(sorted(x for x in seen if x)[-300:], ensure_ascii=False))
    log(results, "자동 읽기")
    return results
