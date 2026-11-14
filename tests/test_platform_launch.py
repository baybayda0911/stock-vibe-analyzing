import importlib.util
import io
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.launch import ensure_server, live_state, startup_lock, stop_server
from app.store import ResearchStore

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "skills/stock-research-lab/scripts/research_lab.py"
spec = importlib.util.spec_from_file_location("p1_launch_fixture_lab", TOOL)
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class PlatformLaunchTests(unittest.TestCase):
    def test_previous_s2_is_recognized_and_not_overwritten_during_upgrade(self):
        with TemporaryDirectory() as temporary:
            root, project = Path(temporary)/'private', Path(temporary)/'project'
            lab.init(root); project.mkdir()
            store=ResearchStore(root, TOOL)
            descriptor=root/'_workspace/platform/server.json'
            descriptor.parent.mkdir(parents=True)
            state={'application':'stock-vibe-analyzing','phase':'S2','pid':1234,'port':12345,
                'instance_id':'fixture-s2','token':'t'*40,'csrf':'c'*40,'root':str(root),'version':'0.11.0',
                'project_dir':str(project),'market_data_dir':None,'lab_tool':str(TOOL)}
            descriptor.write_text(json.dumps(state),encoding='utf-8')
            health=json.dumps({'application':state['application'],'instance_id':state['instance_id']}).encode()
            with patch('app.launch.urllib.request.build_opener') as opener, patch('app.launch.subprocess.Popen') as spawn:
                opener.return_value.open.return_value.__enter__.side_effect=lambda:io.BytesIO(health)
                self.assertEqual(live_state(descriptor,store),state)
                with self.assertRaisesRegex(ValueError,'旧版平台'): ensure_server(root,project,TOOL)
                spawn.assert_not_called()
            self.assertEqual(json.loads(descriptor.read_text()),state)

    def test_previous_p1_service_can_be_identified_for_stopping_but_cannot_be_reused(self):
        with TemporaryDirectory() as temporary:
            root, project = Path(temporary) / "private", Path(temporary) / "project"
            project.mkdir()
            lab.init(root)
            store = ResearchStore(root, TOOL)
            descriptor = root / "_workspace/platform/server.json"
            descriptor.parent.mkdir(parents=True)
            state = {"application": "stock-vibe-analyzing", "phase": "P1", "pid": 1234, "port": 12345,
                "instance_id": "fixture-previous-service", "token": "t" * 40, "csrf": "c" * 40,
                "root": str(root), "version": "0.9.0", "project_dir": str(project), "market_data_dir": None, "lab_tool": str(TOOL)}
            descriptor.write_text(json.dumps(state), encoding="utf-8")
            health = json.dumps({"application": state["application"], "instance_id": state["instance_id"], "phase": "P1", "version": "0.9.0"}).encode()
            with patch("app.launch.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value.__enter__.side_effect = lambda: io.BytesIO(health)
                self.assertEqual(live_state(descriptor, store), state)
                with patch("app.launch.subprocess.Popen") as spawn:
                    with self.assertRaisesRegex(ValueError, "旧版平台"):
                        ensure_server(root, project, TOOL)
                    spawn.assert_not_called()

    def test_reopen_reuses_server_then_stop_and_restart_preserves_watchlist(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "private"
            project = Path(temporary) / "project"
            project.mkdir()
            lab.init(root)
            try:
                first, reused = ensure_server(root, project, TOOL)
                self.assertFalse(reused)
                second, reused = ensure_server(root, project, TOOL)
                self.assertTrue(reused)
                self.assertEqual(first["pid"], second["pid"])
                self.assertEqual(first["instance_id"], second["instance_id"])
                identity = {"security_id": "ZZ-TEST-DEMO", "company": "Fictional", "market": "ZZ", "exchange": "TEST",
                            "code": "DEMO", "share_class": "ordinary", "currency": "TST"}
                lab.ensure_security(root, identity)
                ResearchStore(root, TOOL).update_watchlist(identity["security_id"], True)
                watchlist_bytes = (root / "platform/watchlist.json").read_bytes()
                other = project / "another"
                other.mkdir()
                with self.assertRaises(ValueError):
                    ensure_server(root, other, TOOL)
                self.assertTrue(stop_server(root, TOOL))
                third, reused = ensure_server(root, project, TOOL)
                self.assertFalse(reused)
                self.assertNotEqual(first["instance_id"], third["instance_id"])
                self.assertEqual((root / "platform/watchlist.json").read_bytes(), watchlist_bytes)
                self.assertEqual(json.loads((root / "config.local.json").read_text())["kind"], "private-research-lab")
            finally:
                stop_server(root, TOOL)

    def test_untrusted_runtime_descriptor_is_not_treated_as_a_live_service(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            lab.init(root)
            store = ResearchStore(root, TOOL)
            state = root / "_workspace/platform/server.json"
            state.parent.mkdir(parents=True)
            state.write_text('{"port":1,"token":"should-not-be-used"}')
            self.assertIsNone(live_state(state, store))

    def test_traversal_inputs_are_rejected_before_resolving_or_starting(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "private"
            project = Path(temporary) / "project"
            project.mkdir()
            lab.init(root)
            for supplied_root, supplied_project in [(root / ".." / "private", project),
                                                    (root, project / ".." / "project")]:
                with self.subTest(root=supplied_root, project=supplied_project), patch("app.launch.subprocess.Popen") as spawn:
                    with self.assertRaises(ValueError):
                        ensure_server(supplied_root, supplied_project, TOOL)
                    spawn.assert_not_called()
            with self.assertRaises(ValueError):
                stop_server(root / ".." / "private", TOOL)

    def test_link_evidence_is_checked_before_root_or_project_resolution(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, project, alias = base / "private", base / "project", base / "alias"
            project.mkdir()
            alias.mkdir()
            lab.init(root)
            original_stat, original_resolve = Path.stat, Path.resolve
            for supplied_root, supplied_project, target in [(alias, project, root), (root, alias, project)]:
                def linked_stat(path, *args, **kwargs):
                    info = original_stat(path, *args, **kwargs)
                    if path == alias and not kwargs.get("follow_symlinks", True):
                        return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_nlink=1, st_file_attributes=0)
                    return info
                def resolving(path, *args, **kwargs):
                    return target if path == alias else original_resolve(path, *args, **kwargs)
                with self.subTest(root=supplied_root, project=supplied_project), \
                     patch.object(Path, "stat", autospec=True, side_effect=linked_stat), \
                     patch.object(Path, "resolve", autospec=True, side_effect=resolving), \
                     patch("app.launch.subprocess.Popen") as spawn:
                    with self.assertRaisesRegex(ValueError, "Linked/reparse"):
                        ensure_server(supplied_root, supplied_project, TOOL)
                    spawn.assert_not_called()

    def test_existing_runtime_directories_and_hardlinks_prevent_startup(self):
        for name in ("startup.lock", "server.log", "server.json"):
            for mode in ("directory", "hardlink"):
                with self.subTest(name=name, mode=mode), TemporaryDirectory() as temporary:
                    root, project = Path(temporary) / "private", Path(temporary) / "project"
                    project.mkdir()
                    lab.init(root)
                    target = root / "_workspace/platform" / name
                    target.parent.mkdir(parents=True)
                    if mode == "directory":
                        target.mkdir()
                    else:
                        target.write_text("fictional runtime", encoding="utf-8")
                    original_stat = Path.stat
                    def runtime_stat(path, *args, **kwargs):
                        info = original_stat(path, *args, **kwargs)
                        if mode == "hardlink" and path == target and kwargs.get("follow_symlinks", True):
                            fields = list(info)
                            fields[3] = 2
                            return os.stat_result(fields)
                        return info
                    with patch.object(Path, "stat", autospec=True, side_effect=runtime_stat), \
                         patch("app.launch.subprocess.Popen") as spawn:
                        with self.assertRaisesRegex(ValueError, "ordinary files without links"):
                            ensure_server(root, project, TOOL)
                        spawn.assert_not_called()

    def test_stop_and_live_state_reject_unsafe_descriptor_before_network(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "private"
            lab.init(root)
            store = ResearchStore(root, TOOL)
            descriptor = root / "_workspace/platform/server.json"
            descriptor.parent.mkdir(parents=True)
            descriptor.mkdir()
            with patch("app.launch.urllib.request.build_opener") as network:
                with self.assertRaises(ValueError):
                    live_state(descriptor, store)
                with self.assertRaises(ValueError):
                    stop_server(root, TOOL)
                with self.assertRaises(ValueError):
                    live_state(Path(temporary) / "arbitrary.json", store)
                with self.assertRaises(ValueError):
                    with startup_lock(Path(temporary) / "arbitrary.lock", store):
                        pass
                network.assert_not_called()

    def test_reopening_a_live_service_still_rejects_unsafe_log(self):
        with TemporaryDirectory() as temporary:
            root, project = Path(temporary) / "private", Path(temporary) / "project"
            project.mkdir()
            lab.init(root)
            log = root / "_workspace/platform/server.log"
            log.mkdir(parents=True)
            state = {"version": (REPO / "VERSION").read_text(encoding="utf-8-sig").strip(),
                     "project_dir": str(project), "lab_tool": str(TOOL), "market_data_dir": None}
            with patch("app.launch.live_state", return_value=state), patch("app.launch.subprocess.Popen") as spawn:
                with self.assertRaisesRegex(ValueError, "ordinary files without links"):
                    ensure_server(root, project, TOOL)
                spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
