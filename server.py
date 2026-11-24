"""약품관리 서버 실행: python server.py  (기본 포트 8000)"""
import os
import sys

from app import create_app, maint

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("DRUG_ORDER_PORT", "8000"))
    maint.start_scheduler()
    if "--dev" in sys.argv:
        app.run(host="0.0.0.0", port=port, debug=True)
    else:
        from waitress import serve
        print(f"약품관리 서버 시작: http://localhost:{port}  (이 창을 닫으면 프로그램이 꺼져요)")
        serve(app, host="0.0.0.0", port=port, threads=8)
