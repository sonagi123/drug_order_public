"""eghis 엑셀(통계·등록수가)과 도매상 매출원장 PDF 읽기."""
import io
import re
from datetime import date
from html.parser import HTMLParser

from . import db


# ---------------------------------------------------------------- 엑셀 읽기
class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.in_cell = [], None, [], False

    def handle_starttag(self, tag, attrs):  # noqa: ARG002 (HTMLParser 규약)
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.in_cell, self.cell = True, []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None:
            self.row.append("".join(self.cell).strip())
            self.in_cell = False
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)


def read_rows(data: bytes):
    """xlsx / xls / html(엑셀로 저장된 표) 모두 행 목록으로."""
    if data[:2] == b"PK":
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        return [[c for c in row] for row in ws.iter_rows(values_only=True)]
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        import xlrd
        book = xlrd.open_workbook(file_contents=data)
        sh = book.sheet_by_index(0)
        return [sh.row_values(i) for i in range(sh.nrows)]
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("파일 형식을 알 수 없어요.")
    p = _TableParser()
    p.feed(text)
    if not p.rows:
        raise ValueError("표를 찾지 못했어요. eghis에서 엑셀로 내보낸 파일인지 확인해 주세요.")
    return p.rows


def _num(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def _s(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _table(rows, must):
    for i, r in enumerate(rows):
        cells = [_s(c) for c in r]
        if must in cells:
            idx = {name: j for j, name in enumerate(cells) if name}
            return idx, rows[i + 1:]
    raise ValueError(f"'{must}' 열을 찾지 못했어요. 올바른 파일인지 확인해 주세요.")


def strip_parens(s):
    s = re.sub(r"\[.*?\]", "", s or "")
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\([^()]*\)", "", s)
    return re.sub(r"\(.*$", "", s).replace(")", "")


def short_name(name):
    """eghis 약명 → 짧은 이름: 괄호·_(…) 제거, 밀리그램→mg."""
    n = re.sub(r"_\(.*$", "", name or "")
    n = strip_parens(n)
    n = n.replace("밀리그램", "mg").replace("밀리그람", "mg").replace("마이크로그램", "mcg")
    return re.sub(r"\s+", "", n)


# ---------------------------------------------------------------- 제약사별 발생 통계
def parse_period(filename):
    m = re.search(r"(\d{8})\D+(\d{8})", filename or "")
    if not m:
        return None, None
    f = lambda s: f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return f(m.group(1)), f(m.group(2))


def import_stats(data, filename, start, end, replace=False, rows=None):
    if not start or not end:
        raise ValueError("기간(시작·끝 날짜)을 입력해 주세요.")
    if start > end:
        raise ValueError("시작 날짜가 끝 날짜보다 늦어요.")
    # 새 기간 안에 완전히 들어가는 예전 업로드만 바꿈. 일부만 겹치면 둘 다 두고, 겹친 날은 나중 파일 값을 씀.
    overl = db.q("SELECT * FROM usage_imports WHERE start >= ? AND end <= ?", (start, end))
    if overl and not replace:
        rng = ", ".join(f"{o['start']}~{o['end']}" for o in overl)
        raise ValueError(f"이미 올린 기간과 겹쳐요: {rng}. 같은 기간을 다시 올리려면 '겹치는 기간 바꾸기'를 체크하세요.")
    rows = rows if rows is not None else read_rows(data)
    try:
        idx, body = _table(rows, "약품코드")          # 제약사별 발생 통계
        K = {"code": "약품코드", "name": "약품명칭"}
    except ValueError:
        idx, body = _table(rows, "사용자코드")        # 처방발생 통계 (처방의별로 나뉜 줄은 합침)
        K = {"code": "사용자코드", "name": "처방명칭"}
    for n in (K["code"], K["name"], "총투여량"):
        if n not in idx:
            raise ValueError(f"'{n}' 열이 없어요.")
    conn = db.connect()
    for o in overl:
        conn.execute("DELETE FROM usage WHERE import_id=?", (o["id"],))
        conn.execute("DELETE FROM usage_imports WHERE id=?", (o["id"],))
    cur = conn.execute("INSERT INTO usage_imports(start, end, filename, created_at) VALUES (?,?,?,?)",
                       (start, end, filename, db.now()))
    iid = cur.lastrowid
    added, count = 0, 0
    agg = {}
    for r in body:
        g = lambda k: r[idx[k]] if k in idx and idx[k] < len(r) else None
        code = _s(g(K["code"]))
        if not code:
            continue
        inout = _s(g("원내(외)"))
        if inout and inout != "원내":
            continue
        kind = _s(g("영수증구분"))
        if kind and "약품비" not in kind:  # 진찰료·검사료 등은 약이 아님
            continue
        cat = db.one("SELECT * FROM catalog WHERE code=?", (code,))
        name = re.sub(r"_\([^()]*\)$", "", _s(g(K["name"])))  # 끝의 _(50mg/1정) 규격 표기 제거
        if cat and cat["name"]:
            name = cat["name"]
        maker = _s(g("제약회사")) or (cat["maker"] if cat else "") or ""
        ins_txt = _s(g("급여구분"))
        insured = 0 if ins_txt == "비급여" else 1
        price = _num(g("단가"))
        qty = _num(g("총투여량"))
        a = agg.setdefault(code, {"qty": 0, "amount": 0, "patients": 0, "price": price})
        a["qty"] += qty
        a["amount"] += _num(g("총금액"))
        a["patients"] += int(_num(g("환자수")))
        # 처방발생 통계에는 청구코드·성분코드가 있어서 등록수가 없이도 같은 제품 합치기가 됨
        claim = _s(g("청구코드")) or (cat["claim_code"] if cat else None) or None
        ingr = _s(g("성분코드")) or (cat["ingr_code"] if cat else None) or None
        ex = db.one("SELECT code FROM drugs WHERE code=?", (code,))
        if not ex:
            conn.execute(
                """INSERT INTO drugs(code, name, order_name, maker, claim_code, ingr_code, insured, price, is_new, created_at)
                   VALUES (?,?,?,?,?,?,?,?,1,?)""",
                (code, name, short_name(name), maker, claim, ingr, insured, price if insured else 0, db.now()))
            added += 1
        else:
            conn.execute("""UPDATE drugs SET maker=COALESCE(NULLIF(?,''), maker), insured=?,
                            claim_code=COALESCE(NULLIF(claim_code,''), ?), ingr_code=COALESCE(NULLIF(ingr_code,''), ?) WHERE code=?""",
                         (maker, insured, claim, ingr, code))
            if insured and price:
                conn.execute("UPDATE drugs SET price=? WHERE code=?", (price, code))
    for code, a in agg.items():
        conn.execute("INSERT INTO usage(import_id, code, qty, price, amount, patients) VALUES (?,?,?,?,?,?)",
                      (iid, code, a["qty"], a["price"], a["amount"], a["patients"]))
        count += 1
    conn.commit()
    from .calc import merge_duplicates
    merge_duplicates()
    return {"count": count, "added": added, "replaced": len(overl)}


# ---------------------------------------------------------------- 사용자등록수가
def import_master(data, rows=None):
    rows = rows if rows is not None else read_rows(data)
    idx, body = _table(rows, "사용자코드")
    conn = db.connect()
    n = 0
    for r in body:
        g = lambda k: r[idx[k]] if k in idx and idx[k] < len(r) else None
        code = _s(g("사용자코드"))
        if not code:
            continue
        ins_txt = _s(g("급여구분"))
        insured = 0 if ins_txt == "비급여" else 1
        price = _num(g("의원단가"))
        used = 1 if _s(g("사용여부")).lower() in ("checked", "o", "y", "사용", "true", "1") else 0
        conn.execute(
            """INSERT INTO catalog(code, name, maker, claim_code, ingr_code, insured, price, inout, used)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET name=COALESCE(NULLIF(excluded.name,''), catalog.name),
                 maker=COALESCE(NULLIF(excluded.maker,''), catalog.maker), claim_code=excluded.claim_code,
                 ingr_code=excluded.ingr_code, insured=excluded.insured, price=COALESCE(excluded.price, catalog.price),
                 inout=excluded.inout, used=excluded.used""",
            (code, _s(g("한글명")), _s(g("제조사")), _s(g("청구코드")), _s(g("성분코드")), insured, price,
             _s(g("원내/외")), used))
        conn.execute("UPDATE drugs SET claim_code=?, ingr_code=? WHERE code=?",
                     (_s(g("청구코드")), _s(g("성분코드")), code))
        if insured and price:
            conn.execute("UPDATE drugs SET price=? WHERE code=? AND insured=1", (price, code))
        n += 1
    conn.commit()
    from .calc import merge_duplicates
    merged = merge_duplicates()
    return {"count": n, "merged": merged}


# ---------------------------------------------------------------- 매출원장 PDF
LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+(.+)\s+(\S+)\s+(-?\d+)\s+([\d,]+\.\d{2})\s+(-?[\d,]+)(?:\s+[\d,]+)*\s*$")


def pack_from_spec(spec):
    part = spec.split("/")[-1]
    m = re.match(r"\s*(\d+)", part)
    return int(m.group(1)) if m else None


def _norm(s):
    s = re.sub(r"^향정\)", "", s or "")
    s = s.replace("밀리그램", "mg").replace("밀리그람", "mg").replace("㎎", "mg").replace("마이크로그램", "mcg")
    s = s.replace("캅셀", "캡슐")
    return re.sub(r"\s+", "", s).lower()


def _strength(txt):
    """'0.5g' → '500mg' 처럼 단위를 맞춤."""
    m = re.match(r"([\d.]+)\s*(mg|mcg|g|㎎)", (txt or "").replace(" ", ""))
    if not m:
        return None
    v, u = float(m.group(1)), m.group(2)
    if u == "g":
        v, u = v * 1000, "mg"
    if u == "㎎":
        u = "mg"
    return (f"{v:g}") + u


def ledger_display_name(name, spec):
    """잘린 원장 제품명 → 카톡 주문명: '리보트릴정 (클로나제팜)' + '0.5mg/100T' → '리보트릴정 0.5mg'."""
    n = re.sub(r"^향정\)", "", name)
    n = strip_parens(n).strip()
    n = n.replace("밀리그램", "mg").replace("밀리그람", "mg")
    if not re.search(r"\d", n):
        st = _strength(spec.split("/")[0]) if "/" in spec else None
        if st:
            n = f"{n} {st}"
    return re.sub(r"\s+", " ", n)


def _keys_ledger(name, spec):
    base = strip_parens(re.sub(r"^향정\)", "", name))
    k1 = _norm(base)
    keys = {k1}
    if not re.search(r"\d", k1) and "/" in spec:
        st = _strength(spec.split("/")[0])
        if st:
            keys.add(k1 + st)
    return keys


def _keys_drug(name):
    main = re.sub(r"_\(.*$", "", name)
    strength = re.search(r"_\(([\d.]+\s*(?:mg|mcg|g))", name)
    base = _norm(strip_parens(main))
    keys = {base}
    if not re.search(r"\d", base) and strength and _strength(strength.group(1)):
        keys.add(base + _strength(strength.group(1)))
    return keys


BIZ_RE = re.compile(r"(\d{3}-\d{2}-\d{5})")


def parse_ledger_pdf(data):
    import pdfplumber
    lines, period, biz = [], (None, None), None
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if not biz:
                m = BIZ_RE.search(text)
                biz = m.group(1) if m else None
            m = re.search(r"조회기간\s*:\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", text)
            if m:
                period = (m.group(1), m.group(2))
            for raw in text.splitlines():
                m = LINE_RE.match(raw.strip())
                if not m:
                    continue
                dt, name, spec, qty, price, amt = m.groups()
                lines.append({
                    "date": dt, "name": name.strip(), "spec": spec, "qty": int(qty),
                    "price": float(price.replace(",", "")), "amount": float(amt.replace(",", "")),
                    "is_disc": 1 if name.startswith("할인") else 0,
                })
    if not lines:
        raise ValueError("원장에서 품목 줄을 찾지 못했어요. 지원하는 양식인지 확인해 주세요.")
    return period, lines, biz


def link_ledger_names(ws_id):
    """원장 제품명 ↔ 약 자동 연결(유일하게 맞을 때만) + 포장단위 채우기. 연결 못 한 이름 목록 반환."""
    drugs = db.q("SELECT code, name, pack_src FROM drugs WHERE alias_of IS NULL")
    keymap = {}
    for g in drugs:
        for k in _keys_drug(g["name"]):
            keymap.setdefault(k, set()).add(g["code"])
    names = db.q(
        """SELECT ll.name, ll.spec, MAX(ll.date) AS last FROM ledger_lines ll
           JOIN ledger_imports li ON li.id = ll.import_id
           WHERE li.ws_id=? AND ll.is_disc=0 GROUP BY ll.name, ll.spec ORDER BY last""", (ws_id,))
    conn = db.connect()
    unmatched = []
    for r in names:
        link = db.one("SELECT code FROM name_links WHERE ws_id=? AND ledger_name=?", (ws_id, r["name"]))
        code = link["code"] if link else None
        if not code:
            cands = set()
            for k in _keys_ledger(r["name"], r["spec"]):
                if k in keymap and len(keymap[k]) == 1:
                    cands |= keymap[k]
            if len(cands) == 1:
                code = cands.pop()
                conn.execute("INSERT OR REPLACE INTO name_links(ws_id, ledger_name, code) VALUES (?,?,?)",
                             (ws_id, r["name"], code))
        if not code:
            unmatched.append(r["name"])
            continue
        if code == "-":  # 무시하기로 한 이름
            continue
        conn.execute("UPDATE ledger_lines SET code=? WHERE name=? AND import_id IN (SELECT id FROM ledger_imports WHERE ws_id=?)",
                     (code, r["name"], ws_id))
        pk = pack_from_spec(r["spec"])
        g = db.one("SELECT pack_src, order_name FROM drugs WHERE code=?", (code,))
        if pk and g and g["pack_src"] != "manual":
            conn.execute("UPDATE drugs SET pack=?, pack_label=?, pack_src='ledger' WHERE code=?",
                         (pk, r["spec"], code))
        if r["name"].startswith("향정)"):
            conn.execute("UPDATE drugs SET psy=1 WHERE code=?", (code,))
        if g and (not g["order_name"] or g["order_name"] == short_name(db.one("SELECT name FROM drugs WHERE code=?", (code,))["name"])):
            conn.execute("UPDATE drugs SET order_name=? WHERE code=?", (ledger_display_name(r["name"], r["spec"]), code))
    conn.commit()
    return unmatched


def _date(v):
    if v is None or v == "":
        return None
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float)) and 30000 < v < 80000:  # 엑셀 날짜 숫자
        from datetime import timedelta as _td
        return (date(1899, 12, 30) + _td(days=int(v))).isoformat()
    t = str(v).strip()
    m = re.match(r"(\d{4})[-./]?(\d{1,2})[-./]?(\d{1,2})", t)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


LEDGER_COLS = {
    "date": ("명세일자", "거래일자", "일자", "날짜", "매출일자", "출고일자"),
    "name": ("제품명", "품명", "품목명", "상품명", "약품명", "제 품 명"),
    "spec": ("규격", "포장", "포장단위"),
    "qty": ("수량",),
    "price": ("단가",),
    "amount": ("판매금액", "금액", "합계금액", "공급대가", "매출금액", "합계"),
}


def parse_ledger_rows(rows):
    """엑셀 매출원장: 머리글 줄을 찾아 열을 맞춤. 소계·합계·수금 줄은 건너뜀."""
    hdr_i, idx = None, {}
    for i, r in enumerate(rows[:40]):
        cells = [re.sub(r"\s+", "", _s(c)) for c in r]
        found = {}
        for key, names in LEDGER_COLS.items():
            for j, c in enumerate(cells):
                if c in [n.replace(" ", "") for n in names] and key not in found:
                    found[key] = j
        if "name" in found and "qty" in found and ("amount" in found or "price" in found):
            hdr_i, idx = i, found
            break
    if hdr_i is None:
        raise ValueError("매출원장 표(제품명·수량·금액 열)를 찾지 못했어요.")
    text = " ".join(_s(c) for r in rows[:hdr_i + 1] for c in r)
    biz = BIZ_RE.search(text)
    pm = re.search(r"(\d{4}[-./]\d{2}[-./]\d{2})\s*~\s*(\d{4}[-./]\d{2}[-./]\d{2})", text)
    period = (_date(pm.group(1)), _date(pm.group(2))) if pm else (None, None)
    lines, last_date = [], None
    for r in rows[hdr_i + 1:]:
        g = lambda k: r[idx[k]] if k in idx and idx[k] < len(r) else None
        name = _s(g("name"))
        d = _date(g("date")) or last_date
        if not name or not d or re.search(r"소계|합계|누계|이월|수금", name):
            continue
        qty = _num(g("qty"))
        price = _num(g("price"))
        amount = _num(g("amount")) if "amount" in idx else qty * price
        if not qty and not amount:
            continue
        last_date = d
        lines.append({"date": d, "name": name, "spec": _s(g("spec")), "qty": int(qty), "price": price,
                      "amount": amount, "is_disc": 1 if name.startswith("할인") else 0})
    if not lines:
        raise ValueError("매출원장에서 품목 줄을 찾지 못했어요.")
    return period, lines, biz.group(1) if biz else None


def detect_kind(data, filename):
    """올린 파일이 무엇인지: ledger-pdf / stats / master / ledger-excel."""
    if data[:4] == b"%PDF":
        return "ledger-pdf", None
    rows = read_rows(data)
    head = " ".join(_s(c) for r in rows[:40] for c in r)
    if "총투여량" in head and ("약품코드" in head or "처방명칭" in head):
        return "stats", rows  # 제약사별 발생 통계 / 처방발생 통계
    if "사용자코드" in head and ("의원단가" in head or "한글명" in head):
        return "master", rows
    return "ledger-excel", rows


def import_ledger(data, filename, ws_id=None, rows=None):
    """매출원장(PDF 또는 엑셀). ws_id가 없으면 사업자번호로 도매상 자동 인식.
    같은 도매상의 같은 날짜 줄은 새 파일로 바꿔서, 매주 겹쳐 올려도 두 번 세지 않음."""
    if data[:4] == b"%PDF":
        period, lines, biz = parse_ledger_pdf(data)
        kind = "pdf"
    else:
        period, lines, biz = parse_ledger_rows(rows if rows is not None else read_rows(data))
        kind = "excel"
    ws = None
    was_auto = not ws_id
    if not ws_id and biz:
        ws = db.one("SELECT * FROM wholesalers WHERE biz_no=? AND active=1", (biz,))
    if not ws and ws_id:
        ws = db.one("SELECT * FROM wholesalers WHERE id=?", (ws_id,))
    if not ws:
        raise ValueError("어느 도매상 원장인지 알 수 없어요. 처음 한 번은 도매상을 직접 골라 올려주세요."
                         + (f" (사업자번호 {biz})" if biz else ""))
    ws_id = ws["id"]
    if biz and not ws["biz_no"]:
        db.run("UPDATE wholesalers SET biz_no=? WHERE id=?", (biz, ws_id))
    start = period[0] or min(l["date"] for l in lines)
    end = period[1] or max(l["date"] for l in lines)
    conn = db.connect()
    # 이 도매상의 같은 기간 줄은 지우고 새로 (주마다 겹쳐 올려도 중복 없음)
    removed = conn.execute("""DELETE FROM ledger_lines WHERE date BETWEEN ? AND ?
                              AND import_id IN (SELECT id FROM ledger_imports WHERE ws_id=?)""", (start, end, ws_id)).rowcount
    conn.execute("""DELETE FROM ledger_imports WHERE ws_id=? AND id NOT IN (SELECT DISTINCT import_id FROM ledger_lines)""", (ws_id,))
    cur = conn.execute("INSERT INTO ledger_imports(ws_id, period_start, period_end, filename, created_at) VALUES (?,?,?,?,?)",
                       (ws_id, start, end, filename, db.now()))
    iid = cur.lastrowid
    for l in lines:
        conn.execute("""INSERT INTO ledger_lines(import_id, date, name, spec, qty, price, amount, is_disc)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (iid, l["date"], l["name"], l["spec"], l["qty"], l["price"], l["amount"], l["is_disc"]))
    if not ws["ledger_format"]:
        conn.execute("UPDATE wholesalers SET ledger_format=? WHERE id=?", (kind, ws_id))
    conn.commit()
    unmatched = link_ledger_names(ws_id)
    days = len({l["date"] for l in lines if not l["is_disc"]})
    return {"lines": len(lines), "days": days, "unmatched": unmatched,
            "replaced": removed, "period": f"{start} ~ {end}", "ws": ws["name"], "auto": was_auto}
