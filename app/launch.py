"""Start or reopen this local platform without creating another server."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

REPO = Path(__file__).resolve().parents[1]
_STARTED = {}
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from app.store import ResearchStore
from app import PHASE


@contextmanager
def startup_lock(path, store, timeout=15):
    # OS locks release on crash, unlike a timestamp-based stale-lock deletion.
    if path != store.lab.inside(store.root, "_workspace", "platform", "startup.lock"):
        raise ValueError("启动锁必须位于固定私有工作区")
    if path.exists():
        store._regular(path)
    stream = path.open("a+b")
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    deadline = time.monotonic() + timeout
    locked = False
    try:
        while not locked:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("平台正在启动，请稍后重试")
                time.sleep(0.15)
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def live_state(path, store):
    if path != store.lab.inside(store.root, "_workspace", "platform", "server.json"):
        raise ValueError("服务描述必须位于固定私有工作区")
    if not path.exists():
        return None
    store._regular(path)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        required = {"application", "phase", "pid", "port", "instance_id", "token", "csrf", "root", "version", "project_dir", "market_data_dir", "lab_tool"}
        if (set(state) != required or state["application"] != "stock-vibe-analyzing"
                or state["phase"] not in {"P1", "S1", "S2", "S3", "S4", PHASE} or state["root"] != str(store.root)
                or type(state["port"]) is not int or not 1 <= state["port"] <= 65535
                or not isinstance(state["token"], str) or len(state["token"]) < 32):
            return None
        # Never send the access token while checking an old runtime descriptor.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = f"http://127.0.0.1:{state['port']}/api/health"
        with opener.open(url, timeout=1) as response:
            health = json.load(response)
        if health.get("application") == state["application"] and health.get("instance_id") == state["instance_id"]:
            return state
    except (ValueError, KeyError, TypeError, OSError, urllib.error.URLError):
        pass
    return None


def ensure_server(root, project_dir, lab_tool=None, market_data_dir=None):
    tool = Path(lab_tool or REPO / "skills/stock-research-lab/scripts/research_lab.py")
    store = ResearchStore(Path(root), tool, Path(market_data_dir) if market_data_dir else None)
    project_dir = store.lab.checked_path(project_dir)
    if not project_dir.is_dir():
        raise ValueError("专属项目目录不存在")
    root, project_dir = store.root.resolve(), project_dir.resolve()
    store.root = root
    runtime = store.lab.inside(root, "_workspace", "platform")
    runtime.mkdir(parents=True, exist_ok=True)
    descriptor = store.lab.inside(root, "_workspace", "platform", "server.json")
    lock = store.lab.inside(root, "_workspace", "platform", "startup.lock")
    log = store.lab.inside(root, "_workspace", "platform", "server.log")
    if log.exists():
        store._regular(log)
    with startup_lock(lock, store):
        state = live_state(descriptor, store)
        if state:
            if state["phase"] != PHASE or state["version"] != (REPO / "VERSION").read_text(encoding="utf-8-sig").strip():
                raise ValueError("旧版平台仍在运行，请先正常停止旧服务再启动新版")
            if (state["project_dir"] != str(project_dir) or state["lab_tool"] != str(tool.resolve())
                    or state["market_data_dir"] != (str(Path(market_data_dir).resolve()) if market_data_dir else None)):
                raise ValueError("运行中的平台使用其他配置，请先停止再更换配置")
            return state, True
        interpreter = Path(sys.executable)
        if interpreter.name.casefold() == "pythonw.exe":
            interpreter = interpreter.with_name("python.exe")
        command = [str(interpreter), "-X", "utf8", "-B", "-m", "app.server", "--root", str(root),
                   "--project-dir", str(project_dir), "--lab-tool", str(tool)]
        if market_data_dir:
            command += ["--market-data-dir", str(market_data_dir)]
        if log.exists():
            store._regular(log)
        with log.open("ab") as output:
            flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
            process = subprocess.Popen(command, cwd=REPO, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=output, creationflags=flags,
                                       start_new_session=os.name != "nt")
        _STARTED[str(root)] = process
        for _ in range(80):
            if process.poll() is not None:
                raise RuntimeError("本地服务启动失败，请查看私有工作区的平台日志")
            store.lab.checked_path(descriptor, reject_git=True)
            state = live_state(descriptor, store)
            # Windows virtualenv launchers may proxy a child PID; the health nonce identifies the service.
            if state:
                return state, False
            time.sleep(0.15)
        stop_server(root, tool)
        process.terminate()
        process.wait(timeout=5)
        raise TimeoutError("本地服务启动超时，原档案未改变")


def stop_server(root, lab_tool=None):
    tool = Path(lab_tool or REPO / "skills/stock-research-lab/scripts/research_lab.py")
    store = ResearchStore(Path(root), tool)
    descriptor = store.lab.inside(store.root, "_workspace", "platform", "server.json")
    state = live_state(descriptor, store)
    if not state:
        return False
    origin = f"http://127.0.0.1:{state['port']}"
    request = urllib.request.Request(origin + "/api/shutdown", data=b"{}", method="POST", headers={
        "Cookie": "p1_session=" + state["token"], "Origin": origin, "X-CSRF-Token": state["csrf"],
        "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        if not json.load(response).get("stopping"):
            raise RuntimeError("未收到服务停止回执")
    for _ in range(60):
        if live_state(descriptor, store) is None:
            process = _STARTED.pop(str(store.root), None)
            if process is not None:
                process.wait(timeout=5)
            return True
        time.sleep(0.2)
    raise TimeoutError("服务尚未停止，请稍后重试")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("STOCK_RESEARCH_LAB_ROOT"), type=Path)
    parser.add_argument("--project-dir", default=os.environ.get("STOCK_RESEARCH_PROJECT_DIR"), type=Path)
    parser.add_argument("--lab-tool", type=Path)
    parser.add_argument("--market-data-dir", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    if args.root is None or args.project_dir is None:
        parser.error("需要指定私有档案根和专属项目目录，或使用已配置的本地启动入口")
    if args.stop:
        stopped = stop_server(args.root, args.lab_tool)
        if sys.stdout is not None:
            print(json.dumps({"stopped": stopped}, ensure_ascii=False))
        return
    state, reused = ensure_server(args.root, args.project_dir, args.lab_tool, args.market_data_dir)
    origin = f"http://127.0.0.1:{state['port']}"
    if not args.no_browser:
        webbrowser.open(origin + "/open?token=" + state["token"])
    if sys.stdout is not None:
        print(json.dumps({"running": True, "reused": reused, "origin": origin, "pid": state["pid"], "phase": PHASE}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, TimeoutError):
        message = "个股研究平台未能启动。请确认配置目录和权限，详情见私有工作区 _workspace/platform/server.log。"
        if sys.stderr is None and os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "本地研究平台", 0x10)
        else:
            print(message, file=sys.stderr)
        sys.exit(1)
