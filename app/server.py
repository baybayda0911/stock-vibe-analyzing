"""Authenticated loopback-only research reader, technical dashboard and watchlist service."""
import argparse
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

from .report_view import render_markdown
from .store import ResearchStore
from .research_tasks import ResearchTasks
from .research_materials import ResearchSelectionRequired
from . import PHASE

REPO = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).with_name("static")


class PlatformServer(ThreadingHTTPServer):
    # Normal shutdown must wait for an in-flight atomic watchlist write and its lock cleanup.
    daemon_threads = False

    def __init__(self, address, store, project_dir):
        if address[0] != "127.0.0.1":
            raise ValueError("Only IPv4 loopback is allowed")
        self.store = store
        self.project_dir = str(Path(project_dir).resolve())
        self.token = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.instance_id = secrets.token_hex(16)
        self.version = (REPO / "VERSION").read_text(encoding="utf-8-sig").strip()
        super().__init__(address, Handler)
        try:
            self.research = ResearchTasks(store, Path(project_dir))
        except Exception:
            super().server_close()
            raise

    def shutdown(self):
        self.research.close()
        super().shutdown()

    def server_close(self):
        if hasattr(self, 'research'):
            self.research.close()
        super().server_close()

    @property
    def origin(self):
        return "http://127.0.0.1:" + str(self.server_port)


class Handler(BaseHTTPRequestHandler):
    server_version = "StockResearchLocal/1"
    timeout = 10

    def log_message(self, format, *args):
        # Local reports and bootstrap tokens never enter access logs.
        pass

    def send_data(self, status, data, media="application/json; charset=utf-8", extra=None, report=False):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN" if report else "DENY")
        policy = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
                  if report else "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("Content-Security-Policy", policy)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def fail(self, code, message):
        self.send_data(code, {"error": message})

    def host_ok(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def authenticated(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get("p1_session")
            return value is not None and secrets.compare_digest(value.value, self.server.token)
        except (CookieError, ValueError, TypeError):
            return False

    def request_parts(self):
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or len(self.path) > 3000:
            raise ValueError("Invalid request target")
        return parsed.path, parse_qs(parsed.query, keep_blank_values=True, max_num_fields=12)

    @staticmethod
    def one(query, key, default=""):
        values = query.get(key, [default])
        if len(values) != 1 or len(values[0]) > 512:
            raise ValueError("Invalid request parameter")
        return values[0]

    def do_GET(self):
        if not self.host_ok():
            return self.fail(403, "请求主机不匹配")
        try:
            path, query = self.request_parts()
            if path == "/api/health":
                return self.send_data(200, {"application": "stock-vibe-analyzing", "phase": PHASE, "version": self.server.version, "instance_id": self.server.instance_id})
            if path == "/open":
                if not secrets.compare_digest(self.one(query, "token"), self.server.token):
                    return self.fail(403, "启动链接已失效，请重新打开启动入口")
                return self.send_data(303, b"", extra={"Location": "/", "Set-Cookie": "p1_session=" + self.server.token + "; HttpOnly; SameSite=Strict; Path=/"})
            if not self.authenticated():
                return self.fail(401, "请从本地启动入口打开研究平台")
            if path == "/api/bootstrap":
                overview = self.server.store.overview()
                from .broker_archive import compatibility
                broker_compatibility = compatibility(self.server.store)
                research_providers = self.server.research.providers()
                return self.send_data(200, {**overview, "watchlist": self.server.store.watchlist()["securities"],
                    "broker_compatibility": broker_compatibility,
                    "research_providers": research_providers,
                    "version": self.server.version, "phase": PHASE, "csrf_token": self.server.csrf,
                    "archive_root": str(self.server.store.root),
                    "project": {"directory": self.server.project_dir, "label": "个股研究室", "phase": PHASE},
                    "providers": ["codex", "deepseek"], "provider_labels": {"codex": "Codex 执行", "deepseek": "DeepSeek API"},
                    "capabilities": {"archive_read": True, "watchlist_write": True, "technical_dashboard": True,
                        "fundamental_dashboard": True, "analyst_updates": True, "analyst_fetch": True,
                        "broker_formal_import": broker_compatibility['enabled'], "continuity": True,
                        "continuity_review": True, "new_research": any(row['available'] for row in research_providers), "comparison": True}})
            if path == "/api/comparison":
                if "security_ids" not in query or set(query) - {"security_ids", "asof", "period_basis", "financial_period", "price_window"}:
                    raise ValueError("Select two to five securities with declared comparison options")
                return self.send_data(200, self.server.store.comparison(self.one(query, "security_ids").split(','),
                    asof=self.one(query, "asof") or None, period_basis=self.one(query, "period_basis", "cumulative"),
                    financial_period=self.one(query, "financial_period") or None,
                    price_window=int(self.one(query, "price_window", "60"))))
            if path in {"/api/research-tasks", "/api/research-task"}:
                if path == "/api/research-tasks":
                    if query:
                        raise ValueError("Task list takes no mutable parameters")
                    return self.send_data(200, self.server.research.list())
                if set(query) != {"task_id"}:
                    raise ValueError("Select one persisted research task")
                return self.send_data(200, self.server.research.get(self.one(query, "task_id")))
            if path == "/api/securities":
                result = self.server.store.search(self.one(query, "q"))
                return self.send_data(200, result if isinstance(result, dict) else {"securities": result})
            if path == "/api/context":
                return self.send_data(200, self.server.store.context(self.one(query, "security_id"), module=self.one(query, "module") or None))
            if path == "/api/technical":
                if set(query) - {"security_id", "period", "limit", "date_from", "date_to"}:
                    raise ValueError("Unexpected technical dashboard parameter")
                return self.send_data(200, self.server.store.technical_dashboard(
                    self.one(query, "security_id"), self.one(query, "period", "1d"),
                    limit=int(self.one(query, "limit", "252")),
                    date_from=self.one(query, "date_from") or None, date_to=self.one(query, "date_to") or None))
            if path == "/api/research-roles":
                if set(query) != {"security_id"}:
                    raise ValueError("Research selection requires a security")
                return self.send_data(200, self.server.store.research_roles(self.one(query, "security_id")))
            if path == "/api/fundamental":
                if set(query) != {"security_id"}:
                    raise ValueError("Fundamental projection requires a security")
                return self.send_data(200, self.server.store.fundamental_dashboard(self.one(query, "security_id")))
            if path == "/api/continuity":
                if set(query) != {"security_id"}:
                    raise ValueError("Continuity requires one complete security identity")
                return self.send_data(200, self.server.store.continuity_dashboard(self.one(query, "security_id")))
            if path == "/api/analyst-updates":
                if set(query) - {"security_id", "limit"} or "security_id" not in query:
                    raise ValueError("Invalid analyst update parameters")
                security_id = self.one(query, "security_id")
                value = self.server.store.analyst_updates(security_id, limit=int(self.one(query, "limit", "20")))
                usage = self.server.research.source_status(security_id)
                for source in value.get('cached_sources', []):
                    source['research_usage'] = usage.get(source['source_id'], [])
                return self.send_data(200, value)
            if path in {"/api/analyst-source", "/api/analyst-source-file"}:
                if set(query) != {"security_id", "source_id"}:
                    raise ValueError("Use the explicitly registered source identity")
                from .broker_cache import source_detail, source_file
                security_id, source_id = self.one(query, "security_id"), self.one(query, "source_id")
                if path == "/api/analyst-source":
                    value = source_detail(self.server.store, security_id, source_id)
                    value['research_usage'] = self.server.research.source_status(security_id).get(source_id, [])
                    return self.send_data(200, value)
                return self.send_data(200, source_file(self.server.store, security_id, source_id), "application/pdf",
                                      {"Content-Disposition": 'attachment; filename="broker-report.pdf"'}, report=True)
            if path == "/api/watchlist":
                return self.send_data(200, self.server.store.watchlist())
            if path in {"/api/report", "/api/asset"}:
                report_id = self.one(query, "id")
                relative = self.one(query, "path") if path == "/api/asset" else None
                if path == "/api/report":
                    reader = self.server.store.document_reader(report_id)
                    data, media, name = reader()
                    text = data.decode("utf-8-sig")
                    body = render_markdown(text, report_id, lambda _record, name: reader(name))
                    return self.send_data(200, body, "text/html; charset=utf-8", report=True)
                data, media, name = self.server.store.document(report_id, relative)
                # Original attachments are downloads; archived HTML never inherits the application origin.
                safe_name = "attachment" + Path(name).suffix.lower()
                return self.send_data(200, data, media, {"Content-Disposition": 'attachment; filename="' + safe_name + '"'}, report=True)
            static = {"/": "index.html", "/static/app.css": "app.css", "/static/app.js": "app.js"}
            if path in static:
                file = STATIC / static[path]
                media = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}[file.suffix]
                return self.send_data(200, file.read_bytes(), media + "; charset=utf-8")
            return self.fail(404, "未找到页面或接口")
        except FileNotFoundError:
            return self.fail(404, "档案或附件不存在")
        except FileExistsError:
            return self.fail(409, "已有写入任务，请稍后重试")
        except KeyError:
            return self.fail(404, "没有找到该股票或档案")
        except (ValueError, TypeError, UnicodeError):
            return self.fail(400, "资料或请求未通过校验，请检查档案完整性")
        except OSError:
            return self.fail(503, "本地资料暂时不可访问，请检查文件权限")

    def do_POST(self):
        if not self.host_ok() or not self.authenticated():
            return self.fail(403, "本机访问校验失败")
        csrf = self.headers.get("X-CSRF-Token", "")
        if self.headers.get("Origin") != self.server.origin or not csrf.isascii() or not secrets.compare_digest(csrf, self.server.csrf):
            return self.fail(403, "操作来源校验失败，请重新打开平台")
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            return self.fail(415, "操作需要JSON请求")
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            path, query = self.request_parts()
            research_operations = {"/api/research-start": self.server.research.start, "/api/research-cancel": self.server.research.cancel,
                "/api/research-resume": self.server.research.resume, "/api/research-answer": self.server.research.answer}
            if not 0 < size <= (32768 if path in research_operations else 4096) or self.headers.get("Transfer-Encoding"):
                return self.fail(413, "请求大小不合法")
            if query or path not in {"/api/watchlist", "/api/shutdown", "/api/analyst-refresh", "/api/analyst-fetch", "/api/continuity-review", "/api/analyst-archive", *research_operations}:
                return self.fail(404, "该操作尚未开放")
            body = json.loads(self.rfile.read(size))
            if path in research_operations:
                return self.send_data(200, research_operations[path](body))
            if path == "/api/continuity-review":
                from .continuity_actions import save_review
                return self.send_data(200, save_review(self.server.store, body))
            if path == "/api/analyst-archive":
                if (not isinstance(body, dict) or set(body) != {"security_id", "source_id"}
                        or any(not isinstance(value, str) or not 1 <= len(value) <= 100 for value in body.values())):
                    raise ValueError("Select an existing reviewed source")
                from .broker_archive import archive_source
                return self.send_data(200, archive_source(self.server.store, body['security_id'], body['source_id']))
            if path == "/api/shutdown":
                if body != {}:
                    raise ValueError("Invalid shutdown request")
                self.send_data(200, {"stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path in {"/api/analyst-refresh", "/api/analyst-fetch"}:
                fields = {"security_id"} if path == "/api/analyst-refresh" else {"security_id", "record_id", "url"}
                if (not isinstance(body, dict) or set(body) != fields
                        or any(not isinstance(value, str) or not 1 <= len(value) <= 1500 for value in body.values())):
                    raise ValueError("Invalid analyst operation")
                from .broker_cache import fetch_report, refresh_index
                result = (refresh_index(self.server.store, body['security_id']) if path == "/api/analyst-refresh"
                          else fetch_report(self.server.store, body['security_id'], body['record_id'], body['url']))
                return self.send_data(200, result)
            if not isinstance(body, dict) or set(body) != {"security_id", "tracked"} or not isinstance(body["security_id"], str) or type(body["tracked"]) is not bool:
                raise ValueError("Invalid watchlist update")
            return self.send_data(200, self.server.store.update_watchlist(body["security_id"], body["tracked"]))
        except FileExistsError:
            return self.fail(409, "任务或档案状态已变化，请重新读取后核对；已受理的请求不会重复执行")
        except KeyError:
            return self.fail(404, "未找到该股票，请先检索并确认")
        except ResearchSelectionRequired as error:
            return self.fail(400, str(error))
        except (ValueError, TypeError, UnicodeError):
            return self.fail(400, "操作请求或来源资料未通过核验，请核对证券、来源、截止日期及原任务状态")
        except OSError:
            return self.fail(503, "资料未能读取或保存，请检查本地权限、公开链接及网络；原研究保留")

    def do_OPTIONS(self):
        self.fail(403, "不接受跨站操作")


def serve(root, project_dir, lab_tool=None, market_data_dir=None, port=0):
    store = ResearchStore(Path(root), Path(lab_tool or REPO / "skills/stock-research-lab/scripts/research_lab.py"),
                          Path(market_data_dir) if market_data_dir else None)
    project = store.lab.checked_path(Path(project_dir))
    if not project.is_dir():
        raise ValueError("Project directory must already exist")
    runtime = store.lab.inside(store.root, "_workspace", "platform")
    runtime.mkdir(parents=True, exist_ok=True)
    server = PlatformServer(("127.0.0.1", port), store, project)
    state = {"application": "stock-vibe-analyzing", "phase": PHASE, "pid": os.getpid(), "port": server.server_port,
             "instance_id": server.instance_id, "token": server.token, "csrf": server.csrf,
             "root": str(store.root), "version": server.version, "project_dir": str(project),
             "market_data_dir": str(Path(market_data_dir).resolve()) if market_data_dir else None,
             "lab_tool": str(Path(lab_tool or REPO / "skills/stock-research-lab/scripts/research_lab.py").resolve())}
    destination = store.lab.inside(store.root, "_workspace", "platform", "server.json")
    temporary = runtime / (".server-" + secrets.token_hex(8) + ".tmp")
    store.lab.write_json(temporary, state)
    os.replace(temporary, destination)
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--lab-tool", type=Path)
    parser.add_argument("--market-data-dir", type=Path)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    serve(args.root, args.project_dir, args.lab_tool, args.market_data_dir, args.port)
