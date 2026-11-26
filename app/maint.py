"""백업·복원·업데이트."""
import glob
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime

from . import db

LOCAL_BACKUP_DIR = os.path.join(db.DATA_DIR, "backups")
KEEP = 30


def _copy_db(dest_path):
    src = db.connect()
    dst = sqlite3.connect(dest_path)
    with dst:
        src.backup(dst)
    dst.close()


def _prune(folder):
    files = sorted(glob.glob(os.path.join(folder, "pharmacy_*.db")))
    for f in files[:-KEEP]:
        try:
            os.remove(f)
        except OSError:
            pass


def backup_now(reason="auto"):
    """로컬 data/backups 에 항상 저장하고, 구글 드라이브 폴더가 설정돼 있으면 거기에도 복사."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"pharmacy_{stamp}_{reason}.db"
    os.makedirs(LOCAL_BACKUP_DIR, exist_ok=True)
    local = os.path.join(LOCAL_BACKUP_DIR, name)
    _copy_db(local)
    _prune(LOCAL_BACKUP_DIR)
    result = {"local": local, "drive": None, "error": None}
    folder = db.get_setting("backup_dir").strip()
    if folder:
        try:
            os.makedirs(folder, exist_ok=True)
            dest = os.path.join(folder, name)
            shutil.copy2(local, dest)
            _prune(folder)
            result["drive"] = dest
        except OSError as e:
            result["error"] = f"구글 드라이브 폴더에 저장하지 못했어요: {e}"
    db.set_setting("last_backup", datetime.now().strftime("%Y-%m-%d %H:%M") + ("" if not result["error"] else " (드라이브 실패)"))
    return result


def list_backups():
    out = []
    folders = [LOCAL_BACKUP_DIR]
    bd = db.get_setting("backup_dir").strip()
    if bd:
        folders.append(bd)
    seen = set()
    for f in folders:
        for p in glob.glob(os.path.join(f, "pharmacy_*.db")):
            n = os.path.basename(p)
            if n in seen:
                continue
            seen.add(n)
            out.append({"name": n, "path": p, "where": "구글 드라이브" if f == bd else "이 PC",
                        "size": os.path.getsize(p)})
    return sorted(out, key=lambda x: x["name"], reverse=True)


def restore_from(path_or_bytes):
    backup_now("before_restore")
    tmp = os.path.join(db.DATA_DIR, "restore_tmp.db")
    if isinstance(path_or_bytes, bytes):
        with open(tmp, "wb") as f:
            f.write(path_or_bytes)
    else:
        shutil.copy2(path_or_bytes, tmp)
    test = sqlite3.connect(tmp)
    try:
        test.execute("SELECT COUNT(*) FROM drugs").fetchone()
    except sqlite3.DatabaseError:
        test.close()
        os.remove(tmp)
        raise ValueError("올바른 백업 파일이 아니에요.")
    src = test
    dst = db.connect()
    with dst:
        src.backup(dst)
    src.close()
    os.remove(tmp)


def start_scheduler():
    def loop():
        n = 0
        while True:
            try:
                if n % 6 == 0:  # 30분마다 백업 확인
                    last = db.get_setting("last_backup")
                    if not last.startswith(datetime.now().strftime("%Y-%m-%d")):
                        backup_now("daily")
                from . import autoimport
                autoimport.scan()  # 5분마다 자동 읽기 폴더 확인
            except Exception as e:  # 실패해도 서버가 멈추면 안 됨
                print("scheduler error:", e)
            finally:
                db.close()
            n += 1
            time.sleep(300)
    threading.Thread(target=loop, daemon=True).start()


# ---------------------------------------------------------------- 업데이트 (git)
def _git(*args):
    r = subprocess.run(["git", "-C", db.BASE_DIR, *args], capture_output=True, text=True, timeout=180)
    return r.returncode, (r.stdout + r.stderr).strip()


def version():
    code, out = _git("log", "-1", "--format=%h %cd", "--date=format:%Y-%m-%d %H:%M")
    return out if code == 0 else "알 수 없음 (git 없음)"


PUBLIC_REPO = "https://github.com/sonagi123/drug_order_public"


def _move_to_public(head):
    """예전 비공개 저장소로 설치한 PC를 공개 배포 저장소로 옮김 (한 번만). 데이터(data/)는 git이 건드리지 않음."""
    code, url = _git("remote", "get-url", "origin")
    if code != 0 or "drug_order_public" in url or "sonagi123/drug_order" not in url:
        return None
    _git("remote", "set-url", "origin", PUBLIC_REPO)
    code, out = _git("fetch", "origin", "main")
    if code != 0:
        _git("remote", "set-url", "origin", url.strip())
        return False, "공개 배포판을 받지 못했어요: " + out[-300:]
    code, out = _git("reset", "--hard", "origin/main")
    if code != 0:
        return False, out[-300:]
    _git("branch", "--set-upstream-to=origin/main")
    db.set_setting("prev_commit", head)
    return True, "공개 배포판 저장소로 옮겼어요. 앞으로 업데이트는 여기서 받아요."


_update_lock = threading.Lock()


def update():
    # 버튼이 두 번 눌려도 업데이트는 한 번만 (동시에 돌면 git 참조가 부딪힘)
    if not _update_lock.acquire(blocking=False):
        return False, "이미 업데이트 중이에요. 잠시 뒤 화면을 새로고침해 주세요."
    try:
        return _update()
    finally:
        _update_lock.release()


def _update():
    code, head = _git("rev-parse", "HEAD")
    if code != 0:
        return False, "git을 찾지 못했어요. install.bat으로 설치했는지 확인해 주세요."
    backup_now("before_update")
    moved = _move_to_public(head.strip())
    if moved is not None:
        return moved
    code, out = _git("pull", "--ff-only")
    if code != 0 and "cannot lock ref" in out:  # 다른 git 작업과 겹쳤으면 한 번 더
        time.sleep(2)
        code, out = _git("pull", "--ff-only")
    if code != 0:
        return False, "업데이트 실패: " + out[-400:]
    if "Already up to date" in out or "이미 업데이트" in out:
        return False, "이미 최신 버전이에요."
    db.set_setting("prev_commit", head)
    return True, out[-400:]


def rollback():
    prev = db.get_setting("prev_commit")
    if not prev:
        return False, "되돌릴 이전 버전 기록이 없어요."
    code, out = _git("reset", "--hard", prev)
    if code != 0:
        return False, out
    return True, f"{prev[:7]} 버전으로 되돌렸어요. 다음 [업데이트] 때 다시 최신으로 바뀌어요."
