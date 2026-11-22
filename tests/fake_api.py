"""가짜 K-water API 서버 — 시험용 합성 데이터만 낸다 (실제 시설 아님).

data.go.kr 응답 꼴(response.header / body.items.item)을 흉내 내고, 요청한 시간 범위의 매시 값을 만든다.
SPIKE_AT 시각에는 탁도를 기준 위로 올려 알림이 나는지 본다.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

PLANTS = [("TEST000001", "가짜정수장A(합성)"), ("TEST000002", "가짜정수장B(합성)")]
SPIKE_AT = datetime(2026, 11, 22, 9)          # 이 시각 A 의 탁도 0.9 NTU


def _hours(q) -> list[datetime]:
    st = datetime.strptime(q["stDt"][0], "%Y-%m-%d") + timedelta(hours=int(q["stTm"][0]))
    ed = datetime.strptime(q["edDt"][0], "%Y-%m-%d") + timedelta(hours=int(q["edTm"][0]))
    out, t = [], st
    while t <= ed:
        out.append(t)
        t += timedelta(hours=1)
    return out


class Handler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    empty_above: int | None = None    # 쪽 크기가 이보다 크면 HTTP 200 · 빈 본문 (실제 API 에서 본 동작)

    def do_GET(self):  # noqa: N802
        q = parse_qs(urlparse(self.path).query)
        Handler.calls.append({k: v[0] for k, v in q.items()})
        if Handler.empty_above is not None and int(q.get("numOfRows", ["0"])[0]) > Handler.empty_above:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if q.get("serviceKey", [""])[0] != "fake-key":
            body = {"response": {"header": {"resultCode": "30", "resultMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"}}}
        else:
            items = []
            for t in _hours(q):
                for code, name in PLANTS:
                    turb = 0.9 if (code == "TEST000001" and t == SPIKE_AT) else 0.05
                    items.append({"occrrncDt": t.strftime("%Y%m%d%H"), "fcltyMngNm": name, "fcltyMngNo": code,
                                  "phVal": "7.1", "tbVal": str(turb), "clVal": "0.8"})
            rows, page = int(q.get("numOfRows", ["1000"])[0]), int(q.get("pageNo", ["1"])[0])
            chunk = items[(page - 1) * rows: page * rows]
            body = {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                                 "body": {"items": {"item": chunk}, "totalCount": len(items)}}}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def start() -> tuple[HTTPServer, str]:
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/waterQuality"
