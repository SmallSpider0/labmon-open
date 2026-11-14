"""池库存和回执随原采集连接取回；组内供给不变成普通主机。"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from labmon import config, overview as O, pool_dispatch as D, users


class Observation(unittest.TestCase):
    def test_personal_and_group_supply_share_collection_without_promoting_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "worker"
            home.mkdir()
            # 本地执行真实 fetch shell；替身只替换 GPU 查询程序，不替换批次与分段解析。
            (home / "labmon_worker.py").write_text(
                'import json,os,sys\ns=json.loads(sys.argv[2])\n'
                'assert os.environ["KA_BIN"] == "/shared/keep alive"\n'
                'assert os.environ["LABMON_POOL_PYTHON"] == "/shared/torch/bin/python"\n'
                'print(json.dumps({"node":{"node":s["host"]},"attempts":{"trial":{"phase":"running"}}}))\n')
            hosts = root / "hosts.toml"
            hosts.write_text(f'[[hosts]]\nid="personal"\nworker_home="{home}"\npython="{sys.executable}"\nka_bin="/shared/keep alive"\npool_python="/shared/torch/bin/python"\n')
            env = {"LABMON_STATE": str(root / "state"), "LABMON_HOSTS": str(hosts), "LABMON_LAN": "off"}
            with patch.dict(os.environ, env):
                h = config.active_hosts()[0]
                supplies = {hid: dict(h, id=hid, pool_observe={"host": hid}, pool_lease_end=time.time() + 900) for hid in ("personal", "ad")}
                with patch.object(D, "observation_hosts", return_value=supplies):
                    result = O.collect([], use_ssh=False)
                self.assertEqual([r["id"] for r in result["hosts"]], ["personal"])
                self.assertEqual(set(result["pool_observed"]), {"personal", "ad"})
                for hid in supplies:
                    self.assertEqual(result["pool_observed"][hid]["lease_end"], supplies[hid]["pool_lease_end"])
                who = users.Ident("member", "user", frozenset({"read"}), "token", "")
                self.assertNotIn("pool_observed", users.view(who, result, users.Access(set(), set(), set())))
                self.assertEqual(result["pool_observed"]["ad"]["node"]["node"], "ad")
                self.assertEqual(D.observation("ad")["attempts"]["trial"]["phase"], "running")
                with patch.object(D.time, "time", return_value=time.time() + config.STALE_S + 1):
                    self.assertEqual(D.observation("ad"), {})
                # 新一轮断连不能把旧的空闲资源重新标成新鲜。
                original = O.fetch
                def fetch(host):
                    return (None, "断连") if host["id"] == "ad" else original(host)
                with patch.object(D, "observation_hosts", return_value=supplies), patch.object(O, "fetch", side_effect=fetch):
                    result = O.collect([], use_ssh=False)
                self.assertEqual(result["pool_observed"]["ad"], {})
                self.assertEqual(D.observation("ad"), {})
