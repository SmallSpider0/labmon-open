"""serve 与 wait 的离线测试：本机主机、假队列、端口 0。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402
from fixtures import NO_HOSTS, ROOT, bearer_opener, fake_host, fake_status, kill_procs, login, make_queue, make_token, post_req, queue_procs, register, running_job, usage_row, waiting_job, write_config  # noqa: E402
os.environ["LABMON_LAN"] = "on"                       # 假服务器连不上：别判成本机不在内网
os.environ["LABMON_HOSTS"] = NO_HOSTS                 # 不受本机用户级主机清单影响

from labmon import collect as C  # noqa: E402
from labmon import config as K  # noqa: E402
from labmon import overview as O  # noqa: E402
from labmon import projects as PJ  # noqa: E402
from labmon.waits import wait_check  # noqa: E402
from labmon import waits as W  # noqa: E402
from labmon.config import load, stale_limit  # noqa: E402
from labmon.health import DOWN_S, link_step  # noqa: E402
from labmon.records import log_events, records, told  # noqa: E402
from labmon.state import load_rows, save_json, state_dir  # noqa: E402
from labmon import records as RC  # noqa: E402
from labmon import users as U  # noqa: E402
from labmon import serve as S  # noqa: E402
from labmon.serve import Handler, Listener, Overview, Project, Server  # noqa: E402


class PersonShareTest(unittest.TestCase):
    """按人的公平份额（#23）：项目算在谁名下（fleet.project_people）、调度卡的 person 与 pk、普通用户看不到别人项目的主人名。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LABMON_STATE"] = str(Path(self.tmp.name) / "state")

    def tearDown(self):
        self.tmp.cleanup()

    def test_people_and_sched_view(self):
        from labmon import fleet as F
        make_token("amy", role="user", scopes=("project",))
        make_token("bob", role="user", scopes=("project",))
        PJ.put_config("pa", 'id = "pa"\n', owner="amy")
        PJ.put_config("pb", 'id = "pb"\n', owner="amy")
        PJ.put_config("pc", 'id = "pc"\n', owner="bob")
        PJ.put_config("pd", 'id = "pd"\n')                                              # 没有主人：归管理员
        people = F.project_people(["pa", "pb", "pc", "pd"])
        self.assertEqual(people, {"pa": "amy", "pb": "amy", "pc": "bob", "pd": F.ADMIN_PERSON})
        now = time.time()
        hosts = [fake_host("x", status=dict(share={"wait_cap_min": 30.0, "half_life_h": 24.0, "weight": {}, "use": {"pa": 1.0, "pc": 2.0, "zz": 0.5}, "run": []},
                                            waiting=[waiting_job("w1", "zz", pos={"n": 1})]))]
        v = O.sched_view(hosts, [{"id": p} for p in ("pa", "pb", "pc", "pd")], now, {}, people)
        sp = {p["id"]: p for p in v["projects"]}
        self.assertEqual({k: p["person"] for k, p in sp.items()}, {"pa": "amy", "pb": "amy", "pc": "bob", "pd": F.ADMIN_PERSON, "zz": None})
        self.assertEqual(sp["pa"]["pk"], sp["pb"]["pk"])
        self.assertNotEqual(sp["pa"]["pk"], sp["pc"]["pk"])
        self.assertEqual((sp["pa"]["pk"], sp["zz"]["pk"]), (F.share_key("amy"), None))   # 看板上没有的项目没有人员键（worker 里合起来算一份）
        self.assertNotIn("amy", F.share_key("amy"))                                      # 共享盘上写的是短哈希，不是名字
        # 普通用户：别人项目的主人名去掉，分组用的 pk 照留
        amy = U.Ident("amy", "user", frozenset({"read", "project"}), "token", "k")
        out = U.view(amy, {"sched": v, "hosts": [], "projects": []}, U.Access({"pa", "pb"}, set(), set()))
        sv = {p["id"]: p for p in out["sched"]["projects"]}
        self.assertEqual({k: p["person"] for k, p in sv.items()}, {"pa": "amy", "pb": "amy", "pc": None, "pd": None, "zz": None})
        self.assertEqual(sv["pc"]["pk"], sp["pc"]["pk"])
        self.assertEqual(v["projects"][2]["person"], "bob")                                # 不改原快照

    def test_person_total_counts_hidden_projects(self):
        # 人的用量合计取 worker 完整的 share.use（与 worker 的 person_use 同一口径）：已归档、调度卡上不显示的项目的用量也算在原主人名下
        from labmon import fleet as F
        people = {"pa": "amy", "parch": "amy", "pc": "bob"}                               # parch 已归档：不在 projects 里，用量还在半衰期里
        hosts = [fake_host("x", status=dict(share={"wait_cap_min": 30.0, "half_life_h": 24.0, "weight": {}, "use": {"parch": 10.0, "pc": 1.0, "zz": 0.5}, "run": []}))]
        v = O.sched_view(hosts, [{"id": "pa"}, {"id": "pc"}], time.time(), {}, people)
        sp = {p["id"]: p for p in v["projects"]}
        self.assertNotIn("parch", sp)
        self.assertEqual((sp["pa"]["use"], sp["pa"]["puse"], sp["pc"]["puse"]), (0.0, 10.0, 1.0))
        self.assertEqual(v["use_sum"], 11.5)                                               # 各项目占比的分母也是完整的合计
        self.assertEqual(F.share_key("amy"), sp["pa"]["pk"])

    def test_collect_survives_bad_fleet_conf(self):
        # [fleet] 写错了（无效正则）：认人按没有 [fleet] 降级，整轮采集照样出快照、写调度设置；错误仍由 _notices 报出来
        hf = Path(self.tmp.name) / "hosts.toml"
        hf.write_text('[fleet]\n[[fleet.rules]]\nmatch = "["\nkind = "job"\n')
        PJ.put_config("pa", 'id = "pa"\n', owner="amy")
        with mock.patch.dict(os.environ, {"LABMON_HOSTS": str(hf)}):
            snap = O.collect([{"id": "pa"}], use_ssh=False)
        self.assertIsNotNone(snap)
        self.assertTrue(any("正则" in n for n in snap.get("notices") or []), snap.get("notices"))


class TellTest(unittest.TestCase):
    """页面上的改动告诉实验会话（#121）：records.tell 记一行，told 进全局快照的 wake（按项目、不按主机）；labmon wait 报一次，
    labmon check 列出还没报过的、记成已报（只加不删），之后 wait 不重报；7 天后不再给。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        os.environ["LABMON_STATE"] = str(self.d / "state")

    def tearDown(self):
        self.tmp.cleanup()

    def test_tell_wait_and_check(self):
        from labmon import records as RC
        from labmon.waits import check_told
        now = time.time()
        RC.tell("a1", "rm", "移除了 a1 在 x 上的 2 条清单登记", now - 8 * 86400)          # 过了 7 天：不再给
        m = RC.tell("a1", "move", "把 a1 的 1 条清单登记搬到 b", now - 60)
        RC.tell("demo-b", "pause", "暂停 demo-b", now - 30)
        self.assertTrue(m.startswith("看板页面上 ") and m.endswith("：把 a1 的 1 条清单登记搬到 b"))
        w = RC.told(now)
        self.assertEqual([(x["key"].split(":")[:3], x["projects"], "host" in x) for x in w], [(["page", "move", "a1"], ["a1"], False), (["page", "pause", "demo-b"], ["demo-b"], False)])
        (self.d / "m.toml").write_text(f'out_dir = "{self.d}/a1"\nid = "a1"\n')
        cfg, out = load(self.d / "m.toml"), self.d / "a1"
        out.mkdir()
        save_json(out / "snapshot.json", {"generated": now, "alerts": [{"key": "failed:j", "msg": "任务失败"}], "hosts": []})
        ov, sf = self.d / "ov.json", self.d / "w.json"
        save_json(ov, {"wake": w + [{"key": "borrow_due:x", "projects": None, "host": "x", "msg": "要还"}]})
        self.assertEqual(wait_check(cfg, out, sf, ov), {"failed:j": "任务失败", w[0]["key"]: m})       # 不按主机定向：项目快照里没有那台照样报
        self.assertEqual(wait_check(cfg, out, sf, ov), {})                                         # 只报一次
        # check：只列 wait 还没报过的页面改动，记成已报（别的告警的记账不动）
        RC.tell("a1", "restore", "恢复了项目 a1", now - 10)
        save_json(ov, {"wake": RC.told(now)})
        got = check_told(cfg, sf, ov)
        self.assertEqual(len(got), 1)
        self.assertIn("恢复了项目 a1", got[0])
        self.assertEqual(check_told(cfg, sf, ov), [])
        self.assertEqual(wait_check(cfg, out, sf, ov), {})                                         # check 列过的 wait 不重报，failed:j 照旧算已报


PCFG = """# 我的项目
title = "老标题"   # 页面标题
id = "pc"
interval = 120     # 采集间隔
out_dir = "out"

[[groups]]
id = "E1"
label = "主实验"   # 组名
prefix = "e1_"

# 消融放后面
[[groups]]
id = "abl"
label = "消融"
color = "#123456"
match = "^abl_"

[[markers]]
date = "2026-11-17"
label = "截稿"

[alerts]
gpu_idle_min = 15    # 空闲多久告警
"""


class ProjectConfigTest(unittest.TestCase):
    """页面上改项目的 monitor.toml（#122，config.set_project、POST /project/config）：只动相关行、注释留着，整套 load 校验后原子写入，
    记配置改动、告诉实验会话；顶层 hide_done_hours 是各车道的缺省。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"LABMON_STATE": str(self.d / "state")})
        self.env.start()
        self.cfg = PJ.cfg_file("pc")              # 看板上登记的那份（#161）：页面改的、Project 读的都是它
        self.cfg.parent.mkdir(parents=True)
        self.cfg.write_text(PCFG)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_set_project_keeps_other_lines(self):
        said = K.set_project(self.cfg, {"title": '新"标题"\\', "interval": "60", "auto_groups": False, "hide_done_hours": 6,
                                        "alerts": {"gpu_idle_min": 30, "log_silent_min": "20"},
                                        "groups": [{"id": "abl", "label": "消融 B"}, {"id": "E1", "label": "主实验"}],
                                        "markers": [{"date": "2026-11-18", "label": "截稿"}, {"date": "2026-12-01 12:00", "label": "答辩"}]})
        self.assertEqual(said, ['标题 老标题 → 新"标题"\\', "采集间隔 120 秒 → 60 秒", "车道收起 24 小时 → 6 小时", "自动归组 开 → 关",
                                "GPU 空闲多久告警 15 → 30 分钟", "日志多久没更新告警 0 → 20 分钟", "计划项 abl 改名 消融 → 消融 B",
                                "计划项顺序改成 消融 B、主实验", "里程碑改成 2026-11-18 截稿、2026-12-01 12:00 答辩"])
        text = self.cfg.read_text()
        for keep in ("# 我的项目\n", "   # 页面标题\n", "interval = 60     # 采集间隔\n", 'label = "主实验"   # 组名\n', "# 消融放后面\n",
                     "gpu_idle_min = 30    # 空闲多久告警\n", 'out_dir = "out"\n'):
            self.assertIn(keep, text)
        c = load(self.cfg)
        self.assertEqual((c["title"], c["interval"], c["auto_groups"], c["hide_done_hours"]), ('新"标题"\\', 60, False, 6))
        self.assertEqual((c["alerts"]["gpu_idle_min"], c["alerts"]["log_silent_min"]), (30, 20))
        # 调了顺序：没写颜色的 E1 钉住原来的颜色（第一个），写了的不动；块之间的注释留在原处
        self.assertEqual([(g["id"], g["label"], g["color"]) for g in c["groups"]], [("abl", "消融 B", "#123456"), ("E1", "主实验", K.PALETTE[0])])
        self.assertLess(text.index("# 消融放后面"), text.index('id = "E1"'))
        self.assertEqual([(m["date"], m["label"]) for m in c["markers"]], [("2026-11-18", "截稿"), ("2026-12-01 12:00", "答辩")])
        ev = [e for e in records(cat="config")]
        self.assertEqual([(e["project"], e.get("host"), e.get("hosts")) for e in ev], [("pc", None, None)])
        self.assertIn("看板页面上改了 pc 的 monitor.toml：标题", ev[0]["msg"])
        # 没变的不写；删光里程碑；改回去
        m0 = self.cfg.stat().st_mtime_ns
        self.assertEqual(K.set_project(self.cfg, {"title": '新"标题"\\', "interval": 60, "alerts": {"gpu_idle_min": 30}}), [])
        self.assertEqual(self.cfg.stat().st_mtime_ns, m0)
        self.assertEqual(K.set_project(self.cfg, {"markers": []}), ["里程碑改成 没有"])
        self.assertNotIn("[[markers]]", self.cfg.read_text())
        self.assertEqual(K.set_project(self.cfg, {"markers": [{"date": "2026-11-20", "label": ""}]}), ["里程碑改成 2026-11-20"])
        self.assertEqual([(m["date"], m["label"]) for m in load(self.cfg)["markers"]], [("2026-11-20", "2026-11-20")])

    def test_set_project_codex_cases(self):
        # Codex 审查 #128：①前面的里程碑没写 label、删掉后面的：改前面那块插了一行，删后面的不能还按改前的行号；
        # ②标题里已有转义的引号再改：旧值要按整个 TOML 字符串认，不能在第一个 \" 处断开；计划项、里程碑的名字同理
        self.cfg.write_text(PCFG.replace('[[markers]]\ndate = "2026-11-17"\nlabel = "截稿"\n', '[[markers]]\ndate = "2026-11-17"\n\n[[markers]]\ndate = "2026-11-20"\nlabel = "答辩"\n'))
        self.assertEqual(K.set_project(self.cfg, {"markers": [{"date": "2026-11-17", "label": "截稿"}]}), ["里程碑改成 2026-11-17 截稿"])
        self.assertEqual([(m["date"], m["label"]) for m in load(self.cfg)["markers"]], [("2026-11-17", "截稿")])
        self.assertNotIn("答辩", self.cfg.read_text())
        for title in ['a"b', 'c"d\\e', "f"]:
            K.set_project(self.cfg, {"title": title, "groups": [{"id": "E1", "label": title + "组"}, {"id": "abl", "label": "消融"}],
                                     "markers": [{"date": "2026-11-17", "label": title + "碑"}]})
            c = load(self.cfg)
            self.assertEqual((c["title"], c["groups"][0]["label"], c["markers"][0]["label"]), (title, title + "组", title + "碑"))
        self.assertIn('title = "f"   # 页面标题\n', self.cfg.read_text())
        # Codex 审查 #128 第二轮：③表头行尾带注释的块照样认；④原生日期时间（中间有空格）整个换掉
        self.cfg.write_text(PCFG.replace('[[markers]]\ndate = "2026-11-17"\n', '[[markers]]   # 截稿日\ndate = 2026-11-17 12:00:00\n'))
        self.assertEqual(K.set_project(self.cfg, {"markers": [{"date": "2026-12-01", "label": "截稿"}]}), ["里程碑改成 2026-12-01 截稿"])
        self.assertEqual([(m["date"], m["label"]) for m in load(self.cfg)["markers"]], [("2026-12-01", "截稿")])
        self.assertEqual(K.set_project(self.cfg, {"markers": []}), ["里程碑改成 没有"])
        self.assertEqual(load(self.cfg)["markers"], [])

    def test_set_project_rejects(self):
        before = self.cfg.read_text()
        for bad, why in [({"interval": 5}, "采集间隔要在 30–3600"), ({"interval": "1.5e2"}, None), ({"title": "  "}, "标题不能空"),
                         ({"id": "x"}, "页面上不能改 id"), ({"alerts": {"nope": 1}}, "不能改告警的 nope"), ({"interval": "abc"}, "要填数"),
                         ({"groups": [{"id": "E1", "label": "x"}]}, "对不上"), ({"groups": [{"id": "zz", "label": "x"}, {"id": "E1", "label": "y"}]}, "没有计划项 zz"),
                         ({"markers": [{"date": "明天", "label": "x"}]}, "里程碑日期格式不对"), ({"refresh": 10.5}, "要填整数")]:
            if why is None:
                K.set_project(self.cfg, bad)               # 150 是合法整数：写了再改回去
                K.set_project(self.cfg, {"interval": 120})
                continue
            with self.assertRaises(K.ConfigError) as e:
                K.set_project(self.cfg, bad)
            self.assertIn(why, str(e.exception), bad)
        self.assertEqual(self.cfg.read_text(), before)
        # alerts 写成内联表的不改，叫人改成段
        self.cfg.write_text(PCFG.replace("[alerts]\ngpu_idle_min = 15    # 空闲多久告警\n", "").replace('out_dir = "out"\n', 'out_dir = "out"\nalerts = {gpu_idle_min = 15}\n'))
        with self.assertRaises(K.ConfigError) as e:
            K.set_project(self.cfg, {"alerts": {"gpu_idle_min": 3}})
        self.assertIn("改成 [alerts] 段", str(e.exception))
        # 没有 [alerts] 段：加在末尾
        self.cfg.write_text(PCFG.replace("[alerts]\ngpu_idle_min = 15    # 空闲多久告警\n", ""))
        K.set_project(self.cfg, {"alerts": {"stale_min": 9}})
        self.assertTrue(self.cfg.read_text().endswith("[alerts]\nstale_min = 9\n"), self.cfg.read_text()[-80:])
        # load 的校验挡住改完不对的文本（如 [[queues]] 本来就写坏了的，这里不写）
        self.cfg.write_text(PCFG + '\n[[queues]]\nid = "q"\nhost = "nope"\nroot = "/r"\njobs = "j.txt"\n')
        with self.assertRaises(K.ConfigError) as e:
            K.set_project(self.cfg, {"title": "x"})
        self.assertIn("host=nope", str(e.exception))

    def test_set_project_color_and_field(self):
        # #133：计划项能换色（只认 #rrggbb）；出错带上是哪一项（field），页面标到那一行
        said = K.set_project(self.cfg, {"groups": [{"id": "E1", "label": "主实验", "color": "#112233"}, {"id": "abl", "label": "消融"}]})
        self.assertEqual(said, [f"计划项 主实验 换色 {K.PALETTE[0]} → #112233"])
        self.assertEqual([(g["id"], g["color"]) for g in load(self.cfg)["groups"]], [("E1", "#112233"), ("abl", "#123456")])
        self.assertIn('color = "#112233"', self.cfg.read_text())
        before = self.cfg.read_text()
        for bad, field in [({"groups": [{"id": "E1", "label": "x", "color": "red"}, {"id": "abl", "label": "y"}]}, "groups"), ({"interval": 5}, "interval"),
                           ({"alerts": {"gpu_idle_min": 0}}, "gpu_idle_min"), ({"title": ""}, "title"), ({"markers": [{"date": "明天"}]}, "markers"), ({"id": "x"}, "id")]:
            with self.assertRaises(K.ConfigError) as e:
                K.set_project(self.cfg, bad)
            self.assertEqual(e.exception.field, field, bad)
        self.assertEqual(self.cfg.read_text(), before)

    def test_set_project_auto_groups(self):
        # #150：自动归出的组（带 auto 与颜色）写成 [[groups]]：id、label、prefix、color，接在最后一个 [[groups]] 后面再按页面上的顺序排；
        # 写明组挪动时没写 color 的照旧钉住颜色；只是接在末尾的不算换顺序
        said = K.set_project(self.cfg, {"groups": [{"id": "E1", "label": "主实验"}, {"id": "sm1_", "label": "sm1_", "color": "#D85A30", "auto": True},
                                                   {"id": "abl", "label": "消融"}, {"id": "x5-", "label": "x5 组", "color": "#1d9e75", "auto": True}]})
        self.assertEqual(said, ["自动归出的计划项 sm1_ 写成 [[groups]]（prefix = sm1_，颜色 #D85A30）", "自动归出的计划项 x5 组 写成 [[groups]]（prefix = x5-，颜色 #1d9e75）",
                                "计划项顺序改成 主实验、sm1_、消融、x5 组"])
        text = self.cfg.read_text()
        self.assertIn('[[groups]]\nid = "sm1_"\nlabel = "sm1_"\nprefix = "sm1_"\ncolor = "#D85A30"\n', text)
        self.assertIn("# 消融放后面\n", text)
        c = load(self.cfg)
        self.assertEqual([(g["id"], g["label"], g.get("prefix"), g["color"]) for g in c["groups"]],
                         [("E1", "主实验", "e1_", K.PALETTE[0]), ("sm1_", "sm1_", "sm1_", "#D85A30"), ("abl", "消融", None, "#123456"), ("x5-", "x5 组", "x5-", "#1d9e75")])
        self.assertEqual([C._group_of(n, c["groups"], True) for n in ("sm1_a", "x5-b", "e1_c", "zz_d")], ["sm1_", "x5-", "E1", "zz_"])
        # 只接在末尾：不报换顺序
        said = K.set_project(self.cfg, {"groups": [{"id": g["id"], "label": g["label"]} for g in c["groups"]] + [{"id": "q_", "label": "q_", "color": "#000000", "auto": True}]})
        self.assertEqual(said, ["自动归出的计划项 q_ 写成 [[groups]]（prefix = q_，颜色 #000000）"])
        self.assertEqual([g["id"] for g in load(self.cfg)["groups"]], ["E1", "sm1_", "abl", "x5-", "q_"])
        # 没有 [[groups]] 的：接在文件末尾
        self.cfg.write_text('id = "pc"\nout_dir = "out"\n\n[alerts]\nstale_min = 9\n')
        K.set_project(self.cfg, {"groups": [{"id": "a_", "label": "a_", "color": "#111111", "auto": True}]})
        self.assertTrue(self.cfg.read_text().endswith('stale_min = 9\n\n[[groups]]\nid = "a_"\nlabel = "a_"\nprefix = "a_"\ncolor = "#111111"\n'), self.cfg.read_text())
        self.assertEqual([(g["id"], g["prefix"]) for g in load(self.cfg)["groups"]], [("a_", "a_")])

    def test_set_project_auto_groups_rejects(self):
        # #150：已写明的、不是前缀形状的、没带颜色的不写；排在用 prefix 的写明组前面会抢走它的任务的不写；文件不动
        self.cfg.write_text(PCFG.replace('prefix = "e1_"', 'prefix = "sm1_big"'))
        before = self.cfg.read_text()
        E1, abl = {"id": "E1", "label": "主实验"}, {"id": "abl", "label": "消融"}
        auto = lambda gid, **kw: {"id": gid, "label": gid, "color": "#D85A30", "auto": True, **kw}   # noqa: E731
        for groups, why in [([E1, abl, auto("E1")], "已经写在"), ([E1, abl, auto("sm1")], "不是自动归出"), ([E1, abl, auto("a_b_")], "不是自动归出"),
                            ([E1, abl, auto("sm1_", color=None)], "不是自动归出"), ([E1, abl, auto("sm1_"), auto("sm1_")], "已经写在"),
                            ([E1, abl, auto("sm1_", label="")], "名字不能空"), ([auto("sm1_"), E1, abl], "会把它的任务（前缀 sm1_big）归过来")]:
            with self.assertRaises(K.ConfigError) as e:
                K.set_project(self.cfg, {"groups": groups})
            self.assertIn(why, str(e.exception), groups)
            self.assertEqual(e.exception.field, "groups")
        self.assertEqual(self.cfg.read_text(), before)
        self.assertEqual(K.set_project(self.cfg, {"groups": [E1, auto("sm1_"), abl]})[-1], "计划项顺序改成 主实验、sm1_、消融")

    def test_spec_is_the_one_source(self):
        # 页面的档位、范围与缺省都取 cfg_spec，load 补缺省也取同一张表：两边不会各写一份
        self.cfg.write_text('id = "pc"\nout_dir = "out"\n')
        c, spec = load(self.cfg), K.cfg_spec()
        for k in ("title", "interval", "refresh", "hide_done_hours", "auto_groups"):
            self.assertEqual(c[k], spec[k]["dft"], k)
        for k in ("gpu_idle_util", "gpu_idle_min", "stale_min", "log_silent_min"):
            self.assertEqual(c["alerts"][k], spec[k]["dft"], k)
        self.assertEqual((spec["interval"]["lo"], spec["interval"]["hi"], spec["interval"]["int"], spec["hide_done_hours"]["int"]), (30, 3600, True, False))
        self.assertEqual((spec["title"]["max"], spec["label"]["max"]), (K.TITLE_MAX, K.LABEL_MAX))
        self.assertEqual(Overview.cfg_info(Project("pc"))["spec"], spec)

    def test_cfg_info_marker_ts(self):
        # Codex 审查 #136：不带年份的里程碑由 load 经 parse_time 解析，cfg_info 带上 ts，页面按它填日期框，不另写一套解析
        self.cfg.write_text(PCFG.replace('date = "2026-11-17"', 'date = "11-17"'))
        m = Overview.cfg_info(Project("pc"))["markers"][0]
        self.assertEqual((m["date"], m["ts"]), ("11-17", K.parse_time("11-17")))
        self.assertIsNotNone(m["ts"])

    def test_proj_info_groups(self):
        # 「管理」抽屉的计划项进度：各组几个任务、跑完几个；没归进任何组的（占位任务不算）
        proj = Project("pc")
        proj.snapshot = lambda: {"groups": [{"id": "E1", "label": "主实验", "color": "#1", "auto": False, "status": "运行中", "total": 3, "done": 1, "failed": 1, "running": 1, "queued": 0},
                                            {"id": "x", "label": "x*", "color": "#2", "auto": True, "status": "完成", "total": 1, "done": 1, "failed": 0, "running": 0, "queued": 0}],
                                 "jobs": [{"id": "e1_a", "group": "E1"}, {"id": "solo", "group": None}, {"id": "hold_1", "group": None, "hold": True}], "lanes": [], "alerts": []}
        pi = Overview([proj]).proj_info()[0]
        self.assertEqual(pi["groups"], [{"id": "E1", "label": "主实验", "color": "#1", "auto": False, "total": 3, "done": 1, "failed": 1, "running": 1, "queued": 0},
                                        {"id": "x", "label": "x*", "color": "#2", "auto": True, "total": 1, "done": 1, "failed": 0, "running": 0, "queued": 0}])
        self.assertEqual(pi["ungrouped"], 1)

    def test_hide_done_hours_default(self):
        self.cfg.write_text(PCFG.replace('interval = 120', 'interval = 120\nhide_done_hours = 6') + '\n[[queues]]\nid = "a"\nroot = "/r"\njobs = "a.txt"\n'
                            + '\n[[queues]]\nid = "b"\nroot = "/r"\njobs = "b.txt"\nhide_done_hours = 48\n')
        c = load(self.cfg)
        self.assertEqual([q["hide_done_hours"] for q in c["queues"]], [6, 48])
        self.assertEqual([q["hide_done_hours"] for q in K.with_queues(c, [{"id": "r", "host": "local", "root": "/r", "jobs": "r.txt"}])["queues"]], [6])
        self.cfg.write_text(PCFG + '\n[[queues]]\nid = "a"\nroot = "/r"\njobs = "a.txt"\n')
        self.assertEqual([q["hide_done_hours"] for q in load(self.cfg)["queues"]], [24])

    def test_route_writes_reloads_and_tells(self):
        proj = Project("pc")
        srv = Server(("127.0.0.1", 0), [proj])
        login(self)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        post = lambda b: json.loads(urllib.request.urlopen(post_req(f"{base}/project/config", json.dumps(b).encode())).read())  # noqa: E731
        try:
            info = srv.overview.cfg_info(proj)
            self.assertEqual((info["title"], info["hide_done_hours"], info["interval_fixed"], info["markers"], info["groups"][0]),
                             ("老标题", 24, False, [{"date": "2026-11-17", "label": "截稿", "ts": K.parse_time("2026-11-17")}], {"id": "E1", "label": "主实验", "color": K.PALETTE[0]}))
            self.assertEqual(post({"project": "pc", "set": {"title": "老标题"}}), {"msg": "没有改动"})
            self.assertEqual(told(), [])
            r = post({"project": "pc", "set": {"title": "新标题", "alerts": {"stale_min": 7}}})
            self.assertEqual(r["msg"], "改了 pc 的 monitor.toml：标题 老标题 → 新标题；数据多久算过期 5 → 7 分钟")
            self.assertEqual((proj.cfg["title"], proj.cfg["alerts"]["stale_min"]), ("新标题", 7))      # 当场重读，不等下一轮
            w = told()
            self.assertEqual([(x["key"].split(":")[:3], x["projects"]) for x in w], [(["page", "config", "pc"], ["pc"])])
            self.assertIn("改的是看板上的那份；labmon wait 下一轮把它拉回项目目录", w[0]["msg"])
            self.assertEqual(PJ.get_config("pc")["text"], self.cfg.read_text())          # 改的是登记目录里那份（#161）
            for body, code, why in [({"project": "pc", "set": {"interval": 1}}, 409, "采集间隔"), ({"project": "zz", "set": {}}, 404, "没有项目 zz"),
                                    ({"project": "pc"}, 400, "没说要改什么")]:
                with self.assertRaises(urllib.error.HTTPError) as e:
                    post(body)
                j = json.loads(e.exception.read())
                self.assertEqual((e.exception.code, why in j["error"], j.get("field")), (code, True, "interval" if code == 409 else None))
        finally:
            srv.shutdown()
            srv.server_close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class ServeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        os.environ["LABMON_STATE"] = str(d / "state")
        self.lock, self.cur = queue_procs()
        make_queue(d / "proj", self.lock.pid, self.cur.pid)
        self.cfg = write_config(d, d / "proj", d / "out")
        self.proj = Project(register(self.cfg), interval=3600)
        threading.Thread(target=self.proj.loop, daemon=True).start()
        with self.proj.cond:
            self.proj.cond.wait_for(lambda: self.proj.runs > 0, 60)

    def tearDown(self):
        kill_procs(self.lock, self.cur)
        if hasattr(self, "srv"):
            self.srv.shutdown()
            self.srv.server_close()
        self.tmp.cleanup()

    def start(self):
        self.srv = Server(("127.0.0.1", 0), [self.proj])
        login(self)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()   # shutdown 不用等缺省 0.5 秒的轮询
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def test_missing_lane_progress_unknown(self):
        # 项目探针没读到的车道（collect._lane 的 missing 占位，unfinished=0）：管理抽屉要的未完成、在跑写空值（说不出），不写 0 冒充空着（Codex 审查 #105）
        snap = {"lanes": [{"id": "l1", "host": "here", "root": "/r", "queue": "jobs/q.txt", "label": "车道 1", "missing": True, "unfinished": 0},
                          {"id": "l2", "host": "here", "root": "/r", "queue": "jobs/p.txt", "label": "车道 2", "unfinished": 2}], "jobs": []}
        self.proj.snapshot = lambda: snap
        ln = Overview([self.proj]).proj_info()[0]["lanes"]
        self.assertEqual([(x["unfinished"], x["run"]) for x in ln], [(None, None), (2, 0)])

    def test_overview_keeps_proj_fields(self):
        # 全局快照的 projects 照 proj_info 给的带全：以前 overview.collect 另按一份白名单挑，漏了 #133 加的 groups、ungrouped，
        # 管理抽屉的计划项一直写“这一轮还没读到项目的任务”（#139）；running 只给 eta.json，不进快照
        g = {"id": "sm1", "label": "sm1", "color": "#123", "auto": True, "total": 2, "done": 1, "failed": 0, "running": 1, "queued": 0}
        snap = {"lanes": [], "groups": [g], "jobs": [{"id": "sm1_a", "group": "sm1", "status": "done"}, {"id": "sm1_b", "group": "sm1", "status": "running"},
                                                      {"id": "x", "status": "queued"}, {"id": "hold_1", "hold": True, "status": "queued"}]}
        self.proj.snapshot = lambda: snap
        p = O.collect(Overview([self.proj]).proj_info())["projects"][0]
        self.assertEqual((p["groups"], p["ungrouped"]), ([g], 1))
        self.assertNotIn("running", p)
        self.assertEqual(json.loads((state_dir() / "snapshot.json").read_text())["projects"][0]["groups"], [g])

    def test_routes_and_collect(self):
        base = self.start()
        pid = self.proj.id
        self.assertEqual(pid, "out")                                       # 缺省取 out_dir 的目录名
        O.collect(self.srv.overview.proj_info())                            # 全局页：没有在用主机时只列项目
        home = urllib.request.urlopen(base + "/").read().decode()
        self.assertIn("<title>labmon 工作台</title>", home)
        ov = json.loads(urllib.request.urlopen(base + "/overview.json").read())
        self.assertEqual([p["id"] for p in ov["projects"]], [pid])
        ln = ov["projects"][0]["lanes"]
        self.assertEqual([{k: x[k] for k in ("host", "root", "queue", "label")} for x in ln],
                         [{"host": "here", "root": str(Path(self.tmp.name) / "proj"), "queue": "jobs/q.txt", "label": "车道 1 训练方"}])   # 时间线按它给 worker 推演里的任务标车道名
        self.assertEqual((ln[0]["returned"], ln[0]["run"]), (None, 1))                               # 项目「管理」抽屉：在跑几个、主机归还没有（#93）
        self.assertIsInstance(ln[0]["unfinished"], int)
        cf = ov["projects"][0]["cfg"]                                                              # 项目配置的只读摘要（#93）
        self.assertEqual((cf["path"], cf["out_dir"], cf["interval"], cf["refresh"]), (str(PJ.cfg_file("out")), str(PJ.out_dir("out")), 3600, 60))   # 看板上登记的那份（#161）
        self.assertEqual(set(cf["alerts"]), {"gpu_idle_util", "gpu_idle_min", "stale_min", "log_silent_min"})
        self.assertEqual(cf["stale"], {"s": 3 * 120 + 60, "runs": 3, "pad": 60, "iv": 120})   # 数据过期的实际门槛 max(3×采集间隔, stale_min) + 1 分钟：按 monitor.toml 的间隔（缺省 120），与 labmon wait 同口径，不按 --interval（#141）
        page = urllib.request.urlopen(f"{base}/p/{pid}/").read().decode()
        self.assertIn("<title>测试监控</title>", page)
        snap = json.loads(urllib.request.urlopen(f"{base}/p/{pid}/snapshot.json").read())
        self.assertEqual(snap["schema"], C.SCHEMA)
        self.assertEqual(snap["thresholds"], {"stale_s": 3 * 120 + 60})          # 项目页标红与 labmon wait 同一门槛（#141）
        self.assertEqual((len(snap["spans"]), snap["attempts"]), (1, []))           # 采集在跑的区间、失败尝试（时间线用）
        self.assertEqual((snap["lanes"][0]["root"], snap["lanes"][0]["queue"]), (str(Path(self.tmp.name) / "proj"), "jobs/q.txt"))
        ev = json.loads(urllib.request.urlopen(f"{base}/p/{pid}/events.json").read())
        self.assertEqual((ev["events"], ev["occ"], "storage" in ev), ({"here": []}, {}, False))       # 本项目的存储在项目快照里（#53）
        self.assertEqual((snap["storage"]["dirs"], snap["storage"]["jobs"], snap["storage"]["free"], snap["storage"]["local"]), ([], [], None, {}))
        self.proj.last_end = 0                                             # 跳过“两次立即采集至少隔 10 秒”
        new = json.loads(urllib.request.urlopen(post_req(f"{base}/p/{pid}/collect")).read())
        self.assertGreater(new["generated"], snap["generated"])
        self.assertEqual(self.proj.runs, 2)
        self.assertNotIn("cached", new)
        again = json.loads(urllib.request.urlopen(post_req(f"{base}/p/{pid}/collect")).read())
        self.assertEqual((again["cached"], again["generated"], self.proj.runs), (True, new["generated"], 2))   # 10 秒内再点：没采，标 cached
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/p/nope/")
        self.assertEqual(e.exception.code, 404)

    def test_sched_weight_and_view(self):
        # 页面上改权重：只动 [sched.weight] 里这一行，写 1 删掉；调度卡汇总各主机的占卡、排队、在等、近 1 天平均等多久与优先
        from labmon import projects
        base, hf = self.start(), Path(self.tmp.name) / "hosts.toml"
        for pid in ("a1", "demo-b"):
            if not projects.cfg_file(pid).is_file():
                projects.put_config(pid, f'id = "{pid}"\n')
        hf.write_text('[[hosts]]\nid = "here"\n')
        os.environ["LABMON_HOSTS"] = str(hf)
        try:
            post = lambda b: urllib.request.urlopen(post_req(base + "/sched/weight", json.dumps(b).encode()))  # noqa: E731
            self.assertIn("a1 的调度权重改成 2", json.loads(post({"project": "a1", "weight": 2}).read())["msg"])
            K.set_weight("demo-b", 1.5)
            self.assertEqual(K.sched(), {"weight": {"a1": 2, "demo-b": 1.5}})
            K.set_weight("a1", 1)
            self.assertEqual(K.sched(), {"weight": {"demo-b": 1.5}})
            self.assertIn('[[hosts]]\nid = "here"\n', hf.read_text())                 # 别的行不动
            with self.assertRaises(urllib.error.HTTPError) as e:
                post({"project": "a1", "weight": -1})
            self.assertEqual(e.exception.code, 409)
            with self.assertRaises(urllib.error.HTTPError) as e:              # 优先的到期已经过去：拒绝，提示与命令行同一句
                urllib.request.urlopen(post_req(base + "/sched/focus", json.dumps({"project": "a1", "until": time.time() - 60}).encode()))
            self.assertEqual(e.exception.code, 409)
            self.assertIn("到期时间", json.loads(e.exception.read())["error"])
            # #96：优先、暂停只改主机清单（所有主机，含之后加入的），不连服务器；看板下一轮随 sched.json 写给各在用主机
            sp = lambda act, b: json.loads(urllib.request.urlopen(post_req(f"{base}/sched/{act}", json.dumps(b).encode())).read())["msg"]  # noqa: E731
            until = time.time() + 3600
            self.assertIn("暂停 demo-b，到", sp("pause", {"project": "demo-b", "until": until}))
            self.assertIn("优先 a1，无期限", sp("focus", {"project": "a1"}))
            g = K.sched()
            self.assertEqual((set(g["pause"]), g["focus"]), ({"demo-b"}, {"a1": None}))
            _, put = O._to_worker([], time.time(), g)
            self.assertEqual(put({"id": "later"})["sched.json"]["focus"], {"a1": None})     # 后加入的主机拿到同一份
            self.assertIn("撤销 demo-b 的暂停", sp("resume", {"project": "demo-b"}))
            self.assertIn("撤销 a1 的优先", sp("unfocus", {"project": "a1"}))
            self.assertEqual(K.sched(), {"weight": {"demo-b": 1.5}})
            # #121：页面上暂停、恢复告诉那个项目的实验会话（records.tell）；权重、优先只改排序，不叫醒；本来就没有的撤销不告诉
            sp("resume", {"project": "demo-b"})
            w = told()
            self.assertEqual([(x["key"].split(":")[1:3], x["projects"]) for x in w], [(["pause", "demo-b"], ["demo-b"]), (["resume", "demo-b"], ["demo-b"])])
            self.assertIn("暂停 demo-b，到", w[0]["msg"])
            self.assertIn("不再放行它的新任务", w[0]["msg"])
            self.assertIn("恢复 demo-b（所有主机）", w[1]["msg"])
        finally:
            os.environ["LABMON_HOSTS"] = NO_HOSTS
        now = time.time()
        # 近 1 天平均等待：跑完的取本机存的用量记录（不止 worker 那边最后 60 行），按开始时刻取 24 小时窗口；在跑的取状态
        uf = state_dir() / "usage" / "x.jsonl"
        uf.parent.mkdir(parents=True, exist_ok=True)
        uf.write_text("".join(json.dumps(r) + "\n" for r in [{"t": now - 3000, "s": now - 3600, "n": "d1", "p": "a1", "g": [], "st": "done", "wait": 10.0},
                                                               {"t": now - 3 * 86400 + 60, "s": now - 3 * 86400, "n": "d0", "p": "a1", "g": [], "st": "done", "wait": 99.0}]
                                                              + [{"t": now - 2000 - i, "s": now - 2100 - i, "n": f"o{i}", "p": "b", "g": [], "st": "done", "wait": 1.0} for i in range(70)]))
        hosts = [fake_host("x", done=[usage_row("d2", "a1", now - 300, now - 100, wait=77.0)],       # 快照里的 done 不再单独算（已在用量记录里）
                           status=dict(share={"wait_cap_min": 30.0, "half_life_h": 24.0, "weight": {"a1": 2.0}, "use": {"a1": 3.0}, "run": [["a1", 0.5, now, now + 60]]},
                                       running=[running_job("r", "a1", now - 60, wait=20.0)],
                                       waiting=[waiting_job("w1", "b", pos={"n": 1}), waiting_job("w2", "b", held="pause")],
                                       plan={"lanes": [{"project": "b", "jobs": [{"name": "w1"}, {"name": "w3"}]}]},
                                       overrides=[{"kind": "focus", "target": "a1", "until": now + 600}, {"kind": "pause", "target": "b"},
                                                  {"kind": "pause", "target": "a1", "global": True}])),
                 fake_host("y", status=dict(t=now - 100, share={"wait_cap_min": 30.0, "half_life_h": 24.0, "weight": {}, "use": {}, "run": [["a1", 1.0, now, None]]},
                                            overrides=[{"kind": "focus", "target": "a1"}]))]
        v = O.sched_view(hosts, [{"id": "a1"}], now, {"pause": {"a1": now + 60}})
        self.assertEqual((v["cap_min"], [p["id"] for p in v["projects"]]), (30.0, ["a1", "b"]))     # 在等的别的项目也列上
        a, b = v["projects"]
        # 优先、暂停：项目级的（主机清单，hosts 为 None）取 sw；只在几台单独下的列出主机、到期取最晚（None 为无期限）；状态里的 global 项不重复算
        self.assertEqual((a["w"], a["use"], a["cards"], a["wait_avg"], a["wait_n"], a["focus"], a["paused"]),
                         (2.0, 3.0, 1.5, 15.0, 2, {"until": None, "hosts": ["x", "y"]}, {"until": now + 60, "hosts": None, "by": None}))
        self.assertEqual((b["w"], b["queued"], b["waiting"], b["paused"], b["focus"]), (1, 2, 1, {"until": None, "hosts": ["x"], "by": {"x": None}}, None))
        self.assertEqual((b["wait_avg"], b["wait_n"]), (1.0, 70))

    def test_sched_view_prefers_hosts_weight(self):
        # #280：sched_view 优先合并主机清单最新落盘的 [sched.weight]，不等 worker 上一轮旧 share.weight
        now = time.time()
        hosts = [fake_host("x", status=dict(share={"wait_cap_min": 30.0, "half_life_h": 24.0, "weight": {"a1": 2.0, "b1": 3.0}, "use": {"a1": 1.0, "b1": 1.0}, "run": []}))]
        projs = [{"id": "a1"}, {"id": "b1"}]
        # 1) sw 带 weight：以 hosts.toml 最新值为准，不在 weight 里的立即恢复为 1
        v1 = O.sched_view(hosts, projs, now, {"weight": {"a1": 5}})
        self.assertEqual({p["id"]: p["w"] for p in v1["projects"]}, {"a1": 5, "b1": 1})
        # 2) 所有项目都改回 1（[sched.weight] 清空，sw["weight"] == {}）：不回退到 worker 旧值
        v2 = O.sched_view(hosts, projs, now, {"weight": {}})
        self.assertEqual({p["id"]: p["w"] for p in v2["projects"]}, {"a1": 1, "b1": 1})
        # 3) sw=None 从 hosts.toml 现读：改完即生效，删行即恢复 1
        hf = Path(self.tmp.name) / "hosts.toml"
        old_hf = os.environ.get("LABMON_HOSTS")
        os.environ["LABMON_HOSTS"] = str(hf)
        try:
            hf.write_text('[sched.weight]\na1 = 4\n')
            v3 = O.sched_view(hosts, projs, now)
            self.assertEqual({p["id"]: p["w"] for p in v3["projects"]}, {"a1": 4, "b1": 1})
            hf.write_text("")
            v4 = O.sched_view(hosts, projs, now)
            self.assertEqual({p["id"]: p["w"] for p in v4["projects"]}, {"a1": 1, "b1": 1})
        finally:
            if old_hf is None:
                os.environ.pop("LABMON_HOSTS", None)
            else:
                os.environ["LABMON_HOSTS"] = old_hf

    def test_events_only_this_project(self):
        # 项目页时间线的事件：同机别的项目的作废、对它的暂停不给（会画进本项目的事件带、被当成空档原因），整台主机的照给
        from labmon.serve import proj_events
        log_events([{"kind": "void", "host": "here", "project": "other", "msg": "别人的作废"},
                      {"kind": "override", "host": "here", "okind": "pause", "target": "other", "act": "add", "msg": "暂停 other"},
                      {"kind": "override", "host": "here", "okind": "drain", "act": "add", "msg": "drain"},
                      {"kind": "void", "host": "here", "project": self.proj.id, "msg": "本项目的作废"},
                      # #96：项目级的暂停（所有主机，记录没有 host）并进项目每台主机的事件，别的项目的不给
                      {"kind": "override", "okind": "pause", "target": self.proj.id, "act": "add", "msg": "暂停本项目（所有主机）"},
                      {"kind": "override", "okind": "pause", "target": "other", "act": "add", "msg": "暂停 other（所有主机）"}])
        ev = proj_events(self.proj)["events"]["here"]
        self.assertEqual(sorted(e["msg"] for e in ev), ["drain", "暂停本项目（所有主机）", "本项目的作废"])
        self.assertEqual([e["msg"] for e in ev if e.get("glob")], ["暂停本项目（所有主机）"])              # 时间线按它与单台的分开配对

    def test_global_collect_and_fresh(self):
        # POST /collect：离上一轮不到 10 秒回现有快照并标 cached；之后真采一轮；?fresh 不受 10 秒限制、等请求之后才开始的那一轮
        base, ov = self.start(), self.srv.overview
        threading.Thread(target=ov.loop, daemon=True).start()
        with ov.cond:
            ov.cond.wait_for(lambda: ov.runs > 0, 60)
        post = lambda q="": json.loads(urllib.request.urlopen(post_req(base + "/collect" + q)).read())  # noqa: E731
        first = post()
        self.assertEqual((first["cached"], ov.runs), (True, 1))
        self.assertEqual(json.loads(urllib.request.urlopen(base + "/overview.json").read())["generated"], first["generated"])
        ov.last_end = 0
        new = post()
        self.assertEqual((ov.runs, "cached" in new), (2, False))
        self.assertGreater(new["generated"], first["generated"])
        self.assertEqual([p["id"] for p in new["projects"]], [self.proj.id])
        fresh = post("?fresh=1")                                           # 刚采过：fresh 照样再采（页面发的就是 ?fresh=1）
        self.assertEqual((ov.runs, "cached" in fresh), (3, False))
        self.assertGreater(fresh["generated"], new["generated"])

    def test_collect_reads_json_before_starting(self):
        """两种立即采集都先读请求体，空请求体也可以。"""
        base = self.start()
        for path, collector in (("/collect?fresh", self.srv.overview), (f"/p/{self.proj.id}/collect", self.proj)):
            with self.subTest(path=path), mock.patch.object(collector, "collect_now", return_value=None) as go:
                req = post_req(base + path)
                req.data = b"[]"
                with self.assertRaises(urllib.error.HTTPError) as bad:
                    urllib.request.urlopen(req)
                self.assertEqual(bad.exception.code, 400)
                bad.exception.close()
                go.assert_not_called()
                for body in (b"{}", b""):
                    req = post_req(base + path)
                    req.data = body
                    with self.assertRaises(urllib.error.HTTPError) as failed:
                        urllib.request.urlopen(req)
                    self.assertEqual(failed.exception.code, 504)
                    failed.exception.close()
                self.assertEqual(go.call_count, 2)

    def test_global_collect_bare_fresh(self):
        # 光写 ?fresh（没有 =1）也算 fresh：parse_qs 要 keep_blank_values，否则空值被丢掉、10 秒内只回 cached
        base, ov = self.start(), self.srv.overview
        threading.Thread(target=ov.loop, daemon=True).start()
        with ov.cond:
            ov.cond.wait_for(lambda: ov.runs > 0, 60)
        r = json.loads(urllib.request.urlopen(post_req(base + "/collect?fresh")).read())
        self.assertEqual((ov.runs, "cached" in r), (2, False))


class AuthTest(unittest.TestCase):
    """看板要登录（#160）：没有“来自本机就放行”；会话 Cookie 的属性；公网入口不认平台操作；普通用户与 project 令牌做不了平台操作、看不到组内服务器；
    吊销立即失效、最后一个管理员撤不掉；口令不进日志。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LABMON_STATE": str(Path(self.tmp.name) / "state"), "LABMON_HOSTS": str(Path(self.tmp.name) / "hosts.toml")})
        self.env.start()
        self.iters = mock.patch.object(U, "ITER", 1000)
        self.iters.start()
        U._GHOST.clear()
        snap = {"generated": time.time(), "hosts": [], "projects": [], "colors": {}, "lan": {"ok": True}, "wake": [{"key": "borrow:x"}], "notices": ["租期"],
                "event_kinds": {}, "event_cats": [["link", "连接"], ["op", "平台操作"], ["queue", "队列"]],
                "fleet": {"servers": [{"id": "srv-secret-id"}], "mine": []},
                "disk": {"df": {"free": 1, "used": 1, "total": 2}, "owners": [{"name": "某人甲"}], "alerts": [{"key": "burst:某人甲", "msg": "某人甲暴涨"}]}}
        save_json(state_dir() / "snapshot.json", snap)
        C.write_page("overview.html", snap, state_dir() / "index.html")
        log_events([{"kind": "fleet_renew", "host": "srv-secret-id", "msg": "续租了 srv-secret-id"}, {"kind": "host_down", "host": "a", "msg": "a 掉线"},
                    {"kind": "config", "who": "某人甲", "msg": "共享盘归给某人甲"}])
        self.srv = Server(("127.0.0.1", 0), [])
        self.pub = Listener(("127.0.0.1", 0), self.srv)
        for s in (self.srv, self.pub):
            threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.lan = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.wan = f"http://127.0.0.1:{self.pub.server_address[1]}"

    def tearDown(self):
        for s in (self.srv, self.pub):
            s.shutdown()
            s.server_close()
        self.iters.stop()
        self.env.stop()
        self.tmp.cleanup()

    def call(self, url, body=None, tok=None, cookie=None, method=None, headers=None):
        """一个请求：返回 (状态码, 头, 内容)；不跟随跳转。"""
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = f"{U.COOKIE}={cookie}"
        req = post_req(url, json.dumps(body).encode()) if body is not None else urllib.request.Request(url)
        if method:
            req.method = method
        for k, v in h.items():
            req.add_header(k, v)
        op = bearer_opener(tok, _NoRedirect) if tok else urllib.request.build_opener(_NoRedirect)
        try:
            r = op.open(req, timeout=10)
            return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_problem_reports(self):
        admin = make_token(scopes=("admin",))
        watcher = make_token(scopes=("reports",))
        amy = make_token("amy", role="user", scopes=("project",))
        read = make_token("amy", role="user", scopes=("read",))
        K.hosts_file().write_text('[[hosts]]\nid = "a"\nssh = "remote-a"\nusers = ["amy"]\n')
        PJ.put_config("owned", 'id = "owned"\n', {}, owner="amy")
        body = {"title": "推演异常", "evidence": "证据 <script>\n第二行", "host": "a", "project": "owned",
                "reporter": "boss", "occurred": 0}
        path = self.wan + "/api/report"
        self.assertEqual(self.call(path, body)[0], 401)
        self.assertEqual(self.call(path, body, tok=read)[0], 403)
        self.assertEqual(self.call(path, {**body, "host": "b"}, tok=amy)[0], 403)
        self.assertEqual(self.call(path, {**body, "project": "other"}, tok=amy)[0], 403)
        for invalid in ({"title": []}, {"evidence": " "}, {"occurred": True}, {"occurred": 10**400}, {"project": {}}):
            self.assertEqual(self.call(path, {**body, **invalid}, tok=amy)[0], 400)
        status, _, raw = self.call(path, body, tok=amy)
        self.assertEqual(status, 201)
        rid = json.loads(raw)["report_id"]
        rows_url = self.lan + "/records.json?cat=report"
        self.assertEqual(json.loads(self.call(rows_url, tok=amy)[2])["rows"], [])
        self.assertEqual(self.call(rows_url, tok=watcher)[0], 403)
        row, = json.loads(self.call(rows_url + "&q=" + rid, tok=admin)[2])["rows"]
        self.assertEqual((row["reporter"], row["project"], row["host"], row["occurred"], row["detail"]),
                         ("amy", "owned", "a", 0, body["evidence"]))
        index = self.wan + "/api/reports"
        self.assertEqual(self.call(index, tok=amy)[0], 403)
        self.assertEqual(json.loads(self.call(index, tok=watcher)[2]),
                         {"reports": [{"report_id": rid, "reporter": "amy", "t": row["t"]}]})
        with mock.patch.object(S.LS, "primary", return_value=False):
            self.assertTrue(json.loads(self.call(index, tok=watcher)[2])["standby"])
            self.assertTrue(json.loads(self.call(path, body, tok=amy)[2])["standby"])
        # 每个普通账号 24 小时最多 REPORT_DAY 份（#18）：再交 429，管理员不受限
        with mock.patch.object(RC, "REPORT_DAY", 2):
            self.assertEqual(self.call(path, body, tok=amy)[0], 201)
            status, _, raw = self.call(path, body, tok=amy)
            self.assertEqual(status, 429)
            self.assertIn("24 小时", json.loads(raw)["error"])
            self.assertEqual(self.call(path, {"title": "看板", "evidence": "x"}, tok=admin)[0], 201)
            with mock.patch("labmon.records.time.time", return_value=time.time() + 86400 + 60):
                self.assertEqual(self.call(path, body, tok=amy)[0], 201)       # 过了 24 小时又能交
        self.assertEqual(len(json.loads(self.call(index, tok=watcher)[2])["reports"]), 4)

    def test_user_project_quota(self):
        # 普通用户名下的项目（含已归档的）最多 USER_PROJECTS 个（#18）：再登记新的回 409；已登记的照常推，管理员不受限
        amy = make_token("amy", role="user", scopes=("project",))
        admin = make_token("boss", role="admin", scopes=("project",))
        put = lambda pid, tok, **kw: self.call(self.lan + f"/api/project/{pid}/config", {"text": f'id = "{pid}"\n', "files": {}}, tok=tok, method="PUT", **kw)   # noqa: E731
        with mock.patch.object(S.U, "USER_PROJECTS", 2):
            self.assertEqual(put("e1", amy)[0], 200)
            status, hd, _ = put("e2", amy)
            self.assertEqual(status, 200)
            PJ.archive("e1", {"generated": time.time(), "no_work_since": time.time() - 100, "lanes": []}, {"generated": time.time(), "hosts": [], "regs": []})
            status, _, raw = put("e3", amy)
            self.assertEqual(status, 409)
            self.assertIn("2 个", json.loads(raw)["error"])
            self.assertFalse(PJ.cfg_file("e3").is_file())
            self.assertEqual(put("e2", amy, headers={"If-Match": hd["ETag"]})[0], 200)   # 已登记的照常推
            self.assertEqual(put("e4", admin)[0], 200)

    def test_object_checks_live_in_own(self):
        """对象鉴权只在 _own 定（#19）：登记新项目的名额、撤别人下的暂停不靠业务函数各自补；业务函数在锁里再核用的是 _own 给的同一份 guard。"""
        amy = make_token("amy", role="user", scopes=("project",))
        admin = make_token("boss", role="admin", scopes=("admin", "project"))
        PJ.put_config("exp", 'id = "exp"\n', owner="amy")
        put = lambda pid: self.call(self.lan + f"/api/project/{pid}/config", {"text": f'id = "{pid}"\n', "files": {}}, tok=amy, method="PUT")   # noqa: E731
        with mock.patch.object(S.U, "USER_PROJECTS", 1), mock.patch.object(S.PJ, "put_config") as stub:
            status, _, raw = put("e2")
            self.assertEqual(status, 409, raw)
            stub.assert_not_called()
        self.assertEqual(self.call(self.lan + "/sched/pause", {"project": "exp"}, tok=admin)[0], 200)
        with mock.patch.object(S.A, "override") as ov, mock.patch.object(S, "set_sched_ovr") as sso:
            for path, data in (("/api/override/resume", {"target": "exp"}), ("/sched/resume", {"project": "exp"})):
                self.assertEqual(self.call(self.lan + path, data, tok=amy)[0], 403, path)
            ov.assert_not_called()
            sso.assert_not_called()
        self.assertEqual(self.call(self.lan + "/api/override/resume", {"target": "exp"}, tok=admin)[0], 200)

    def test_minimum_client_and_version_metadata(self):
        code = fixtures.add_account("boss", "admin", "本机命令行")
        _, headers, _ = self.call(self.lan + "/api/setup", {"name": "boss", "code": code, "password": "correct horse"})
        sid = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        raw, _ = U.token_add("boss", "命令行", ["project"])
        self.srv.min_client_commit = "a" * 40
        version = {"X-Labmon-Client": "b" * 40}
        with mock.patch("labmon.serve.DI.client_status", return_value="below_minimum"):
            status, _, body = self.call(self.lan + "/api/hosts", tok=raw, headers=version)
            self.assertEqual(status, 426)
            self.assertEqual(json.loads(body)["reason"], "client_outdated")
            self.assertEqual(json.loads(body)["commit"], S.DP.RUNNING["commit"])     # 在跑的提交：wait 自动更新时核对下载的版本（#17）
            self.assertEqual(self.call(self.lan + "/api/hosts", tok=raw)[0], 200)
            self.assertEqual(self.call(self.lan + "/api/hosts", cookie=sid, headers=version)[0], 200)
            self.assertEqual(self.call(self.lan + "/api/me", tok=raw, headers=version)[0], 200)
            token = json.loads(self.call(self.lan + "/api/me", cookie=sid)[2])["tokens"][0]
            self.assertEqual(token["client_commit"], "b" * 40)
            self.assertEqual(token["client_status"], "below_minimum")
            self.assertGreater(token["client_seen"], 0)
            self.assertNotIn(raw, json.dumps(token))
            with mock.patch("labmon.serve.LS.primary", return_value=False):
                status, _, body = self.call(self.lan + "/api/hosts", tok=raw, headers=version)
                self.assertEqual(status, 409)
                self.assertTrue(json.loads(body)["standby"])

    def test_unknown_client_version_is_not_refused(self):
        # #17：比看板新、分支上跑、冷备回退后的客户端不在运行版本的祖先链上（unknown）：只提示，不当太旧拒绝
        raw = make_token(scopes=("project",))
        self.srv.min_client_commit = "a" * 40
        with mock.patch("labmon.serve.DI.client_status", return_value="unknown"):
            self.assertEqual(self.call(self.lan + "/api/hosts", tok=raw, headers={"X-Labmon-Client": "c" * 40})[0], 200)

    def test_login_required_even_from_localhost(self):
        # 以前本机来的请求一律放行（trust_local）：现在两个入口都要登录
        for base in (self.lan, self.wan):
            code, h, _ = self.call(base + "/")
            self.assertEqual((code, h["Location"]), (302, "login?next="))
            code, h, _ = self.call(base + "/p/x/")
            self.assertEqual((code, h["Location"]), (302, "../../login?next=p/x/"))
            for path in ("/overview.json", "/records.json", "/api/me", "/fleet/op.json"):
                self.assertEqual(self.call(base + path)[0], 401, path)
            self.assertEqual(self.call(base + "/collect", {})[0], 401)
        code, _, body = self.call(self.lan + "/login")
        self.assertEqual(code, 200)
        self.assertIn('"empty": true', body.decode())                     # 还没有账号：页面上写怎么在本机建第一个管理员

    def test_setup_login_logout_cookie(self):
        code = fixtures.add_account("boss", "admin", "本机命令行")
        st, h, body = self.call(self.lan + "/api/setup", {"name": "boss", "code": code, "password": "correct horse"})
        self.assertEqual(st, 200, body)
        ck = h["Set-Cookie"]
        self.assertRegex(ck, r"^labmon_s=[^;]+; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict$")   # 内网入口没配证书：不加 Secure（配了的见 test_public.TlsTest）
        sid = ck.split(";")[0].split("=", 1)[1]
        me = json.loads(self.call(self.lan + "/api/me", cookie=sid)[2])
        self.assertEqual((me["name"], me["admin"], me["ops"], me["write"], me["entry"], len(me["sessions"])), ("boss", True, True, True, "lan", 1))
        page = self.call(self.lan + "/", cookie=sid)[2].decode()
        self.assertIn('let ME = {"name": "boss"', page)                     # 发页面时填进 ME
        self.assertIn("srv-secret-id", page)                                # 管理员看得到组内服务器
        self.assertEqual(self.call(self.lan + "/login?next=p/x/", cookie=sid)[1]["Location"], "./p/x/")    # 已登录：直接回去
        self.assertEqual(self.call(self.lan + "/login?next=//evil.example", cookie=sid)[1]["Location"], "./")
        st, h, _ = self.call(self.wan + "/api/login", {"name": "boss", "password": "correct horse"})
        self.assertEqual(st, 200)
        self.assertTrue(h["Set-Cookie"].endswith("; Secure"))               # 公网入口只经 HTTPS 反代用
        st, h, _ = self.call(self.lan + "/api/logout", {}, cookie=sid)
        self.assertEqual((st, "Max-Age=0" in h["Set-Cookie"]), (200, True))
        self.assertEqual(self.call(self.lan + "/api/me", cookie=sid)[0], 401)   # 退出后会话作废

    def test_public_platform_permission_switch(self):
        admin = make_token()
        body = {"name": "boss", "enabled": True}
        self.assertEqual(self.call(self.wan + "/api/users/public-ops", body, tok=admin)[0], 404)
        self.assertEqual(self.call(self.lan + "/api/users/public-ops", body, tok=admin)[0], 200)
        fake = lambda handler, *args: handler._json(200, {"ok": True})
        with mock.patch.object(Handler, "_fleet_op", fake):
            self.assertEqual(self.call(self.wan + "/fleet/op/sync", {}, tok=admin)[0], 200)
            self.assertTrue(json.loads(self.call(self.wan + "/api/me", tok=admin)[2])["ops"])
            for path in ("/api/admin/deploy", "/api/worker/start"):
                self.assertEqual(self.call(self.wan + path, {}, tok=admin)[0], 404)
            self.call(self.lan + "/api/users/public-ops", {**body, "enabled": False}, tok=admin)
            self.assertEqual(self.call(self.wan + "/fleet/op/sync", {}, tok=admin)[0], 403)

    def test_backup_and_accounts_only_on_lan(self):
        # #17：冷备与账号管理只在内网入口（公网入口当没这条路由，开了公网平台许可也一样）；冷备拉取本来就走内网入口（docs/deploy.md）
        admin, backup = make_token(), make_token(scopes=("backup",))
        self.call(self.lan + "/api/users/public-ops", {"name": "boss", "enabled": True}, tok=admin)
        self.assertEqual(self.call(self.wan + "/api/admin/backup", tok=backup)[0], 404)
        self.assertEqual(self.call(self.wan + "/api/users", tok=admin)[0], 404)
        for act in ("add", "role", "reset", "revoke", "remove", "hosts"):
            self.assertEqual(self.call(self.wan + "/api/users/" + act, {"name": "amy"}, tok=admin)[0], 404, act)
        self.assertEqual(self.call(self.lan + "/api/users", tok=admin)[0], 200)
        with mock.patch("labmon.serve.BK.make_bytes", return_value=("b.tar.gz", b"x")):
            self.assertEqual(self.call(self.lan + "/api/admin/backup", tok=backup)[0], 200)

    def test_public_entry_has_no_platform_ops(self):
        hit = []
        fake = lambda self, *a: (hit.append(self.path), self._json(200, {"ok": True}))   # noqa: E731
        admin = make_token()
        with mock.patch.object(Handler, "_fleet_op", fake), mock.patch.object(Handler, "_probe", fake), mock.patch.object(Handler, "_renew", fake):
            for path in ("/fleet/op/sync", "/fleet/probe/s1", "/renew/a/reserve"):
                for tok in (admin, None):                                    # 公网入口：登没登录都当没这条路由
                    code, _, body = self.call(self.wan + path, {}, tok=tok)
                    self.assertEqual(code, 403 if tok else 401, path)
                self.assertEqual(self.call(self.lan + path, {}, tok=admin)[0], 200, path)
            proj = make_token(scopes=("project",))
            user = make_token("amy", role="user", scopes=("read",))
            for tok in (proj, user):                                         # project 令牌、普通用户：平台操作 403
                self.assertEqual(self.call(self.lan + "/fleet/op/renew", {}, tok=tok)[0], 403)
            self.assertEqual(hit, ["/fleet/op/sync", "/fleet/probe/s1", "/renew/a/reserve"])
        self.assertEqual(self.call(self.lan + "/collect", {}, tok=user)[0], 403)
        self.assertEqual(self.call(self.lan + "/sched/pause", {"project": "x"}, tok=user)[0], 403)
        self.assertEqual(self.call(self.lan + "/sched/weight", {"project": "x"}, tok=user)[0], 403)
        me = json.loads(self.call(self.wan + "/api/me", tok=admin)[2])
        self.assertEqual((me["admin"], me["ops"], me["entry"]), (True, False, "public"))     # 管理员从公网入口来：页面上那些位置写“要在内网入口做”

    def test_fleet_identity_uses_authenticated_account(self):
        Path(os.environ["LABMON_HOSTS"]).write_text('[fleet]\nme = "legacy"\n')
        first, second = make_token("boss"), make_token("other")
        for token, name in ((first, "boss"), (second, "other")):
            me = json.loads(self.call(self.lan + "/api/me", tok=token)[2])
            self.assertEqual(me["fleet_person"], name)
        with mock.patch("labmon.serve.FB.preview", return_value={}) as plan:
            self.assertEqual(self.call(self.lan + "/fleet/plan.json?person=somebody", tok=second)[0], 200)
            self.assertEqual(plan.call_args.kwargs["account"], "other")
            self.assertIsNone(plan.call_args.kwargs["spec"])
            self.assertEqual(self.call(self.lan + "/fleet/plan.json?kind=move&sid=b&spec=sB", tok=second)[0], 200)
            self.assertEqual(plan.call_args.args[1:3], ("move", "b"))
            self.assertEqual(plan.call_args.kwargs["spec"], "sB")
        with mock.patch.object(S.F, "conf", return_value={"servers": []}), mock.patch.object(S.L, "check", return_value={"ok": True}), \
                mock.patch.object(S.FO, "start") as start, mock.patch.object(S.FO, "view", return_value={"ok": True}):
            self.assertEqual(self.call(self.lan + "/fleet/op/move", {"ids": ["b"], "spec": "sB", "ack": ["b"]}, tok=second)[0], 200)
            self.assertEqual((start.call_args.args[0], start.call_args.kwargs["ids"], start.call_args.kwargs["spec"],
                              start.call_args.kwargs["ack"], start.call_args.kwargs["account"]),
                             ("move", ["b"], "sB", ["b"], "other"))
            self.assertEqual(self.call(self.lan + "/fleet/op/swap", {"ids": ["b"], "person": "DD", "spec": "sB"}, tok=second)[0], 200)
            self.assertEqual((start.call_args.args[0], start.call_args.kwargs["spec"]), ("swap", "sB"))
            self.assertEqual(self.call(self.wan + "/fleet/op/move", {"ids": ["b"], "spec": "sB"}, tok=second)[0], 403)
        with mock.patch("labmon.serve.A.borrow", return_value={"rc": 0}) as borrow:
            self.assertEqual(self.call(self.lan + "/api/borrow", {"id": "s", "account": "boss"}, tok=second)[0], 200)
            self.assertEqual(borrow.call_args.kwargs["account"], "other")

    def test_account_people_conflicts_do_not_merge_or_change_access(self):
        """账号库里两个账号绑到同一人（手改出来的）：两边都不认这人，管理员照样能登录、管账号；再给这人开账号 409（#19：只认账号库的绑定）。"""
        from labmon import fleetboot as FB
        boss, other = make_token("boss"), make_token("other")
        FB.set_person("OWNER", create=True, roster_only=True)
        with S.U._edit() as d:
            d["users"]["boss"]["fleet_person"] = "owner"
            d["users"]["other"]["fleet_person"] = "OWNER"
        rc, _, body = self.call(self.lan + "/api/users/add", {"name": "owner", "person": "OWNER"}, tok=boss)
        self.assertEqual(rc, 409)
        self.assertIn("已开通账号", json.loads(body)["error"])
        rc, _, body = self.call(self.lan + "/api/users", tok=boss)
        self.assertEqual(rc, 200)
        for account in json.loads(body)["users"]:
            self.assertIsNone(account["fleet_person"])
            self.assertIn("对应多个账号", account["fleet_person_error"])
        rc, _, body = self.call(self.lan + "/api/me", tok=other)
        self.assertEqual(rc, 200)
        self.assertIsNone(json.loads(body)["fleet_person"])
        S.U.bind_person("boss", "boss", "本机命令行")                          # 冲突的一方改绑别人：另一方就认得出了
        self.assertEqual(json.loads(self.call(self.lan + "/api/me", tok=other)[2])["fleet_person"], "OWNER")
        read = S.U.token_add("boss", "读页面", ["read"])[0]
        self.assertEqual(self.call(self.lan + "/api/users", tok=read)[0], 403)

    def test_platform_post_reads_body_before_operation(self):
        admin = make_token()
        cfg = {"servers": [{"id": "s1"}]}
        cases = (("/fleet/probe/s1", {"servers": [{"id": "s1"}]}), ("/renew/a/reserve", {"msg": "已预备"}))
        with mock.patch.object(S.F, "conf", return_value=cfg), mock.patch.object(S.L, "check", return_value={"ok": True}), \
                mock.patch.object(self.srv.fleet, "tick") as probe, mock.patch.object(S.FV, "view", return_value=cfg), \
                mock.patch.object(S, "active_hosts", return_value=[{"id": "a"}]), mock.patch.object(S.R, "reserve", return_value="已预备") as reserve:
            for path, expected in cases:
                with self.subTest(path=path):
                    probe.reset_mock()
                    reserve.reset_mock()
                    url = self.lan + path
                    for body, headers in (([], {}), ({}, {"Content-Length": "-1"}), ({}, {"Content-Length": "bad"})):
                        self.assertEqual(self.call(url, body, tok=admin, headers=headers)[0], 400)
                    self.assertEqual(self.call(url, [], tok=None)[0], 401)
                    with mock.patch.object(S.LS, "primary", return_value=False):
                        status, _, body = self.call(url, [], tok=admin)
                        self.assertEqual(status, 409)
                        self.assertTrue(json.loads(body)["standby"])
                    probe.assert_not_called()
                    reserve.assert_not_called()
                    for body in ({}, None):
                        status, _, reply = self.call(url, body, tok=admin, method="POST", headers={"Content-Type": "application/json"})
                        self.assertEqual((status, json.loads(reply)), (200, expected))
                    acted = probe if path.startswith("/fleet/") else reserve
                    self.assertEqual(acted.call_count, 2)

    def test_user_sees_no_fleet(self):
        user = make_token("amy", role="user", scopes=("read",))
        K.hosts_file().write_text('[[hosts]]\nid = "a"\nssh = "remote-a"\nusers = ["amy"]\n')
        ov = json.loads(self.call(self.lan + "/overview.json", tok=user)[2])
        self.assertEqual((ov["fleet"], ov["wake"], ov["notices"], ov["disk"]),
                         (None, [], [], {"df": {"free": 1, "used": 1, "total": 2}, "person": "amy", "owners": [], "alerts": []}))
        self.assertIsNone(ov["pool"])
        self.assertEqual([c[0] for c in ov["event_cats"]], ["link", "queue"])
        page = self.call(self.lan + "/", tok=user)[2].decode()
        self.assertNotIn("srv-secret-id", page)                             # 现渲染的：不发磁盘上带全量数据的 index.html
        self.assertNotIn("某人甲", page)
        self.assertIn('"role": "user"', page)
        for path in ("/fleet/op.json", "/fleet/s1.json", "/fleet/person/x.json", "/fleet/plan.json", "/api/users"):
            self.assertEqual(self.call(self.lan + path, tok=user)[0], 403, path)
        rec = json.loads(self.call(self.lan + "/records.json?cats=1", tok=user)[2])
        self.assertEqual([r["msg"] for r in rec["rows"]], ["a 掉线"])
        self.assertEqual(rec["cats"], {"link": 1})
        admin = make_token()
        self.assertEqual(len(json.loads(self.call(self.lan + "/records.json", tok=admin)[2])["rows"]), 3)

    def test_member_portal_routes_and_isolation(self):
        """#211：普通用户可读自己持有服务器曲线与本人共享盘；他人与不存在 ID 统一 403；换人或改绑后旧持有人立即 403；管理路由与平台写路由均 403。"""
        from labmon import fleetboot as FB
        FB.set_person("OWNER", create=True, roster_only=True)
        FB.set_person("OTHER", create=True, roster_only=True)
        K.hosts_file().write_text(
            '[fleet]\n'
            '[[fleet.servers]]\nid = "s-own"\nholder = "OWNER"\nssh = "root@192.0.2.10"\nport = 15101\npool = "p1"\n'
            '[[fleet.servers]]\nid = "s-other"\nholder = "OTHER"\nssh = "root@192.0.2.11"\nport = 15102\npool = "p1"\n'
        )
        amy = make_token("amy", role="user", scopes=("read", "project"))
        bob = make_token("bob", role="user", scopes=("read", "project"))
        with S.U._edit() as d:
            d["users"]["amy"]["fleet_person"] = "OWNER"
            d["users"]["bob"]["fleet_person"] = "OTHER"
        admin = make_token("boss")
        # 1. 曲线路由：本人 200，他人与不存在 ID 统一 403（防 ID 枚举）
        with mock.patch.object(S.FV, "detail", return_value={"id": "s-own", "series": [{"t": 1}]}):
            rc, _, body = self.call(self.lan + "/fleet/s-own.json", tok=amy)
            self.assertEqual((rc, json.loads(body)["id"]), (200, "s-own"))
            self.assertEqual(self.call(self.lan + "/fleet/s-own.json", tok=admin)[0], 200)
        for path in ("/fleet/s-other.json", "/fleet/s-ghost.json", "/fleet/op.json", "/fleet/plan.json",
                     "/fleet/person/OWNER.json", "/fleet/person/OTHER.json"):
            self.assertEqual(self.call(self.lan + path, tok=amy)[0], 403, path)
        # 平台写路由仍 403
        for path in ("/fleet/op/renew", "/fleet/op/off", "/fleet/op/swap", "/fleet/probe/s-own", "/renew/s-own/reserve"):
            self.assertEqual(self.call(self.lan + path, {}, tok=amy)[0], 403, path)
        # 2. 换人后旧持有人立即 403，新持有人立即 200
        K.hosts_file().write_text(
            '[fleet]\n'
            '[[fleet.servers]]\nid = "s-own"\nholder = "OTHER"\nssh = "root@192.0.2.10"\nport = 15101\npool = "p1"\n'
            '[[fleet.servers]]\nid = "s-other"\nholder = "OTHER"\nssh = "root@192.0.2.11"\nport = 15102\npool = "p1"\n'
        )
        self.assertEqual(self.call(self.lan + "/fleet/s-own.json", tok=amy)[0], 403)
        with mock.patch.object(S.FV, "detail", return_value={"id": "s-own", "series": []}):
            self.assertEqual(self.call(self.lan + "/fleet/s-own.json", tok=bob)[0], 200)
        # 3. 改绑 fleet_person 后旧绑定立即失效
        FB.set_person("NEWP", create=True, roster_only=True)
        with S.U._edit() as d:
            d["users"]["bob"]["fleet_person"] = "NEWP"
        self.assertEqual(self.call(self.lan + "/fleet/s-own.json", tok=bob)[0], 403)
        # 4. /api/me 对普通用户返回本人 fleet_person，不泄露冲突报错
        me_amy = json.loads(self.call(self.lan + "/api/me", tok=amy)[2])
        self.assertEqual(me_amy["fleet_person"], "OWNER")
        self.assertNotIn("fleet_person_error", me_amy)
        # 5. GET /api/disk：仅返回总盘 + 本人目录；备用节点回 409 standby
        save_json(state_dir() / "snapshot.json", {
            "generated": time.time(),
            "disk": {
                "root": "/data/share", "df": {"total": 1000 * K.GIB, "used": 400 * K.GIB, "free": 600 * K.GIB},
                "eta": 15.0, "warn_gb": 30, "names": ["OWNER", "OTHER"], "now": "/data/share/other/secret",
                "owners": [
                    {"name": "OWNER", "public": False, "b": 100 * K.GIB, "d24": 5 * K.GIB, "d7": 10 * K.GIB, "cold": 20 * K.GIB,
                     "units": [{"u": "owner/exp", "b": 100 * K.GIB, "d24": 5 * K.GIB, "d7": 10 * K.GIB, "n": 42}]},
                    {"name": "OTHER", "public": False, "b": 300 * K.GIB, "units": [{"u": "other/secret", "b": 300 * K.GIB}]},
                ],
                "alerts": [{"key": "low", "msg": "剩余低"}, {"key": "burst:OWNER", "msg": "OWNER 涨快"}, {"key": "burst:OTHER", "msg": "OTHER 涨快"}],
            },
        })
        rc, _, body = self.call(self.lan + "/api/disk", tok=amy)
        self.assertEqual(rc, 200, body)
        dk = json.loads(body)
        self.assertIn("OWNER", dk["out"])
        self.assertIn("owner/exp", dk["out"])
        self.assertNotIn("OTHER", dk["out"])
        self.assertNotIn("other/secret", json.dumps(dk, ensure_ascii=False))
        self.assertEqual([o["name"] for o in dk["data"]["owners"]], ["OWNER"])
        with mock.patch.object(S.LS, "primary", return_value=False):
            rc, _, body = self.call(self.lan + "/api/disk", tok=amy)
            self.assertEqual((rc, json.loads(body).get("standby")), (409, True))

    def test_project_owner_and_host_permissions(self):
        admin = make_token()
        amy = make_token("amy", role="user", scopes=("project",))
        bob = make_token("bob", role="user", scopes=("project",))
        K.hosts_file().write_text('[[hosts]]\nid = "a"\nssh = "remote-a"\n[[hosts]]\nid = "b"\nssh = "root@127.0.0.1"\n')
        body = {"text": 'id = "exp"\n', "files": {}}
        uri = self.lan + "/api/project/exp/config"
        status, hd, payload = self.call(uri, body, tok=amy, method="PUT")
        self.assertEqual(status, 200, payload)
        self.assertEqual(PJ.owner("exp"), "amy")
        self.assertEqual(self.call(uri, tok=amy)[0], 200)
        self.assertEqual(self.call(uri, body, tok=bob, method="PUT", headers={"If-Match": hd["ETag"]})[0], 403)
        for path, data in (("/project/config", {"project": "exp", "set": {}}),
                           ("/project/archive", {"project": "exp"}),
                           ("/project/restore", {"project": "exp"}),
                           ("/sched/pause", {"project": "exp"}),
                           ("/api/wait", {"project": "exp"}),
                           ("/api/wait", {"project": "exp", "jobs": ["other/job"]})):
            self.assertEqual(self.call(self.lan + path, data, tok=bob)[0], 403, path)
        self.assertEqual(self.call(self.lan + "/p/exp/snapshot.json", tok=bob)[0], 403)
        self.assertEqual(self.call(self.lan + "/collect", {}, tok=amy)[0], 403)
        self.assertEqual(self.call(self.lan + "/api/users/hosts", {"name": "amy", "hosts": ["b"]}, tok=admin)[0], 409)
        self.assertEqual(self.call(self.lan + "/api/users/hosts", {"name": "amy", "hosts": ["a"]}, tok=admin)[0], 200)
        users = json.loads(self.call(self.lan + "/api/users", tok=admin)[2])
        self.assertEqual([h["id"] for h in users["hosts"]], ["a"])
        self.assertEqual([h["id"] for h in json.loads(self.call(self.lan + "/api/hosts", tok=amy)[2])["hosts"]], ["a"])
        self.assertEqual(self.call(self.lan + "/project/owner", {"project": "exp", "owner": "bob"}, tok=admin)[0], 200)
        self.assertEqual(self.call(uri, tok=amy)[0], 403)
        self.assertEqual(self.call(uri, tok=bob)[0], 200)

    def test_queue_stop_path_is_checked_for_both_roles_before_dispatch(self):
        admin = make_token("boss", role="admin", scopes=("project",))
        amy = make_token("amy", role="user", scopes=("project",))
        PJ.put_config("exp", 'id = "exp"\n', owner="amy")
        K.hosts_file().write_text('[[hosts]]\nid = "a"\nssh = "remote-a"\nusers = ["amy"]\n[[hosts]]\nid = "b"\nssh = "remote-b"\nusers = ["amy"]\n')
        first = {"host": "a", "project": "exp", "root": "/root", "jobs": "q.txt", "stop": "term"}
        save_json(state_dir() / "snapshot.json", {"regs": [{"host": "a", "queues": [first]}]})
        with mock.patch.object(S.W, "run_out", return_value=(0, "完成")) as run, mock.patch.object(S.L, "check", return_value={"ok": True}):
            for token in (admin, amy):
                for stop in ("file:../outside", "file:/outside", "file:.", "file:", "file:a/../b", "file:a\x00b", 0, "other"):
                    item = dict(first, stop=stop)
                    data = {"host": "a", "project": "exp", "args": ["/root", "q.txt"], "stop": stop}
                    self.assertEqual(self.call(self.lan + "/api/worker/add", data, tok=token)[0], 400)
                    self.assertEqual(self.call(self.lan + "/reg/move", {"to": "b", "items": [first, item]}, tok=token)[0], 400)
            run.assert_not_called()
            for token in (admin, amy):
                data = {"host": "a", "project": "exp", "args": ["/root", "q.txt"], "stop": "file:signals/stop"}
                self.assertEqual(self.call(self.lan + "/api/worker/add", data, tok=token)[0], 200)
                self.assertEqual(run.call_args.kwargs["stop"], "file:signals/stop")

    def test_reg_move_pool_expects_source(self):
        # 算力池搬（pool 不为 None）带上原来那台给 worker 在锁里核对（--expect-from）；页面上的搬不带（Codex 审查 #75）
        PJ.put_config("exp", 'id = "exp"\n')
        it = {"host": "pool", "project": "exp", "root": "/root", "jobs": "q.txt", "stop": "term"}
        with mock.patch.object(S.W, "run_out", return_value=(0, "完成")) as run, mock.patch.object(S, "tell"):
            S.reg_move("g", [it], pool="")
            self.assertEqual(run.call_args.kwargs.get("expect_from"), "pool")
            S.reg_move("g", [it])
            self.assertIsNone(run.call_args.kwargs.get("expect_from"))

    def test_reg_move_pool_no_tell(self):
        # #88、#212：算力池放、搬、回池只记 sched 分类下的 pool 事件，不写 tell.jsonl（不叫醒会话）；页面上的搬照旧告诉
        PJ.put_config("exp", 'id = "exp"\n')
        it = {"host": "pool", "project": "exp", "root": "/root", "jobs": "q.txt", "stop": "term"}
        n0 = len(records(cat="sched"))
        with mock.patch.object(S.W, "run_out", return_value=(0, "完成")):
            S.reg_move("g", [it], pool="pool")
            S.reg_move("pool", [dict(it, host="g")], pool="pool", pool_reason="g 已还")
            self.assertEqual(told(), [])
            self.assertEqual(len(records(cat="sched")) - n0, 2)          # 事件照记并归入调度分类
            S.reg_move("g", [it])
        self.assertEqual([x["key"].split(":")[1] for x in told()], ["move"])

    def test_host_assign_permissions_and_behavior(self):
        # #173：无登记自用主机「给项目用」：路由鉴权（need_host + need_project）、主备租约（lease.need）、仅自用主机、经 records.tell 通知实验会话
        admin = make_token("boss", role="admin", scopes=("project",))
        amy = make_token("amy", role="user", scopes=("project",))
        bob = make_token("bob", role="user", scopes=("project",))
        PJ.put_config("exp", 'id = "exp"\n', owner="amy")
        PJ.put_config("other", 'id = "other"\n', owner="bob")
        PJ.put_config("arch", 'id = "arch"\n', owner="amy")
        now = time.time()
        PJ.archive("arch", {"generated": now, "no_work_since": now, "jobs": []}, {"generated": now, "hosts": [], "regs": []}, 60)
        K.hosts_file().write_text(
            '[[hosts]]\nid = "a"\nssh = "remote-a"\nusers = ["amy"]\n'
            '[[hosts]]\nid = "b"\nssh = "remote-b"\n'
            '[[hosts]]\nid = "swap1"\nssh = "remote-s"\nusers = ["amy"]\n[hosts.borrow]\nholder = "bob"\nsince = 1\nswap = true\n'
            '[[hosts]]\nid = "lent1"\nssh = "remote-l"\nusers = ["amy"]\n[hosts.borrow]\nholder = "bob"\nsince = 1\n'
            '[[hosts]]\nid = "old"\nssh = "remote-o"\nreturned = "2026-11-01 00:00"\n'
        )
        # 参数校验（管理员与普通用户一致返回 400）
        for tok in (admin, amy):
            for bad in ({}, {"host": "", "project": "exp"}, {"host": "a", "project": " "},
                        {"host": 1, "project": "exp"}, {"host": "a\x00", "project": "exp"}):
                self.assertEqual(self.call(self.lan + "/host/assign", bad, tok=tok)[0], 400, bad)
        # 普通用户越权：无主机权限或非项目主人 -> 403
        self.assertEqual(self.call(self.lan + "/host/assign", {"host": "b", "project": "exp"}, tok=amy)[0], 403)
        self.assertEqual(self.call(self.lan + "/host/assign", {"host": "a", "project": "other"}, tok=amy)[0], 403)
        self.assertEqual(self.call(self.lan + "/host/assign", {"host": "a", "project": "exp"}, tok=bob)[0], 403)
        # 非自用主机、已归还/不存在主机、已归档/未登记项目 -> 409
        for data in ({"host": "lent1", "project": "exp"}, {"host": "old", "project": "exp"},
                     {"host": "nope", "project": "exp"}, {"host": "a", "project": "arch"},
                     {"host": "a", "project": "missing"}):
            self.assertEqual(self.call(self.lan + "/host/assign", data, tok=admin)[0], 409, data)
        self.assertEqual(told(), [])
        # 管理员、普通用户、换人换给自己的自用主机（borrow.swap）均成功，写 tell.jsonl 与事件并叫醒 overview
        n0 = len(records(cat="config"))
        self.srv.overview.wake.clear()
        status, _, body = self.call(self.lan + "/host/assign", {"host": "a", "project": "exp"}, tok=amy)
        self.assertEqual(status, 200, body)
        self.assertIn("已通知 exp 的实验会话使用 a", json.loads(body)["msg"])
        self.assertTrue(self.srv.overview.wake.is_set())
        self.assertEqual(self.call(self.lan + "/host/assign", {"host": "swap1", "project": "exp"}, tok=amy)[0], 200)
        self.assertEqual(self.call(self.lan + "/host/assign", {"host": "b", "project": "other"}, tok=admin)[0], 200)
        ts = told()
        self.assertEqual([(x["key"].split(":")[1], x["projects"]) for x in ts],
                         [("host", ["exp"]), ("host", ["exp"]), ("host", ["other"])])
        self.assertIn("labmon worker add a ROOT JOBS --project exp", ts[0]["msg"])
        self.assertEqual(len(records(cat="config")) - n0, 3)
        # 备用看板拒绝（HTTP 409 standby，直接调用 host_assign 也过 lease.need）
        with mock.patch.object(S.LS, "primary", return_value=False):
            status, _, body = self.call(self.lan + "/host/assign", {"host": "a", "project": "exp"}, tok=admin)
            self.assertEqual(status, 409, body)
            self.assertTrue(json.loads(body).get("standby"))
            with self.assertRaises(K.ConfigError):
                S.host_assign("a", "exp")

    def test_worker_and_override_authorize_executed_parameters(self):
        amy = make_token("amy", role="user", scopes=("project",))
        PJ.put_config("exp", 'id = "exp"\n', owner="amy")
        K.hosts_file().write_text('[[hosts]]\nid = "a"\nssh = "remote-a"\nusers = ["amy"]\n[[hosts]]\nid = "b"\n')
        save_json(state_dir() / "snapshot.json", {"regs": [
            {"host": "a", "home": "/real", "queues": [{"root": "/root", "jobs": "own.txt", "project": "exp"},
                                                     {"root": "/root", "jobs": "other.txt", "project": "private"}]},
        ]})
        with mock.patch.object(S.W, "run_out", return_value=(0, "完成")) as run:
            for path, data, expected in (
                ("/api/worker/add", {"host": "b", "project": "exp", "args": ["/root", "q.txt"]}, 403),
                ("/api/worker/add", {"host": "a", "project": "exp", "args": ["/root", "other.txt"]}, 403),
                ("/api/worker/add", {"host": "a", "project": "exp", "args": ["/root", "./other.txt"]}, 403),      # JOBS 换个写法也认得（#18）
                ("/api/worker/add", {"host": "a", "project": "exp", "args": ["/root/", "x/../other.txt"]}, 403),
                ("/api/worker/rm", {"host": "a", "args": ["/root", "other.txt"]}, 403),
                ("/api/worker/rm", {"host": "a", "args": ["/root", "./other.txt"]}, 403),
                ("/api/worker/rm", {"host": "a", "args": ["/", "root/other.txt"]}, 403),                        # 从别处断开也是同一条（#77）
                ("/api/worker/add", {"host": "a", "project": "exp", "args": ["/", "root/other.txt"]}, 403),
                ("/api/worker/rm", {"host": "a", "args": ["/root", "own.txt", "--host=b"]}, 400),
                ("/api/worker/add", {"host": "a", "project": "exp", "args": ["/root", "q.txt", "--project=private"]}, 400),
                ("/api/worker/add", {"host": "a", "project": "exp", "args": ["/root", "q.txt"], "stop": "file:../outside"}, 400),
                ("/api/worker/fit", {"host": "a", "args": ["--host=b", "task"]}, 400),
                ("/api/worker/status", {"host": "a", "args": "--host=b"}, 400),
                ("/api/project/init", {"host": "a", "name": "../x"}, 400),
                ("/api/override/pause", {"target": "exp", "hosts": ["a"], "until": "1; false"}, 400),
                ("/api/override/pause", {"target": "exp", "now": True}, 403),
                ("/api/override/pause", {"target": "exp", "hosts": ["b"]}, 403),
            ):
                self.assertEqual(self.call(self.lan + path, data, tok=amy)[0], expected, path)
            run.assert_not_called()
            self.assertEqual(self.call(self.lan + "/api/worker/rm", {"host": "a", "args": ["/root", "own.txt"]}, tok=amy)[0], 200)
            self.assertEqual(run.call_args.kwargs["expect_project"], "exp")
            self.assertEqual(self.call(self.lan + "/api/worker/rm", {"host": "a", "args": ["/", "root/own.txt"]}, tok=amy)[0], 200)   # #77
            self.assertEqual(run.call_args.kwargs["expect_project"], "exp")
            with mock.patch.object(S.L, "check", return_value={"ok": True}):
                self.assertEqual(self.call(self.lan + "/reg/rm", {"host": "a", "home": "/fake", "project": "exp",
                                      "items": [{"root": "/root", "jobs": "own.txt"}]}, tok=amy)[0], 200)
            self.assertEqual(run.call_args.kwargs, {"home": None, "expect_project": "exp"})

    def test_focus_is_admin_only(self):
        """优先、撤销优先只给管理员（#16）：页面与命令行两个入口，带不带 --host 都一样；普通用户的项目也不行。"""
        amy = make_token("amy", role="user", scopes=("project",))
        admin = make_token("boss", role="admin", scopes=("admin", "project"))
        PJ.put_config("exp", 'id = "exp"\n', owner="amy")
        K.hosts_file().write_text('[[hosts]]\nid = "a"\nssh = "remote-a"\nusers = ["amy"]\n')
        with mock.patch.object(S.W, "override_each", return_value=[]) as each:
            for path, data in (("/sched/focus", {"project": "exp"}), ("/sched/unfocus", {"project": "exp"}),
                               ("/api/override/focus", {"target": "exp"}), ("/api/override/unfocus", {"target": "exp"}),
                               ("/api/override/focus", {"target": "exp", "hosts": ["a"]})):
                self.assertEqual(self.call(self.lan + path, data, tok=amy)[0], 403, path)
            each.assert_not_called()
        self.assertNotIn("focus", K.sched())
        self.assertEqual(self.call(self.lan + "/sched/focus", {"project": "exp"}, tok=admin)[0], 200)
        self.assertIn("exp", K.sched()["focus"])
        self.assertEqual(self.call(self.lan + "/api/override/unfocus", {"target": "exp"}, tok=admin)[0], 200)
        self.assertNotIn("focus", K.sched())

    def test_pause_records_who_and_user_cannot_undo_admin_pause(self):
        """项目级暂停记谁下的（#16）：管理员下的普通用户撤不掉、也不能重下一次改成自己的；自己下的自己能撤。页面与命令行同一条规则。"""
        amy = make_token("amy", role="user", scopes=("project",))
        admin = make_token("boss", role="admin", scopes=("admin", "project"))
        PJ.put_config("exp", 'id = "exp"\n', owner="amy")
        K.hosts_file().write_text('[[hosts]]\nid = "a"\nssh = "remote-a"\nusers = ["amy"]\n')
        later = time.time() + 3600
        self.assertEqual(self.call(self.lan + "/sched/pause", {"project": "exp"}, tok=admin)[0], 200)
        self.assertEqual(K.pause_by(), {"exp": "boss"})
        for path, data in (("/sched/resume", {"project": "exp"}), ("/api/override/resume", {"target": "exp"}),
                           ("/sched/pause", {"project": "exp", "until": later}), ("/api/override/pause", {"target": "exp", "until": later})):
            self.assertEqual(self.call(self.lan + path, data, tok=amy)[0], 403, path)
        self.assertEqual(K.sched()["pause"], {"exp": None})                  # 还是管理员那条：无期限、记着管理员
        self.assertEqual(K.pause_by(), {"exp": "boss"})
        self.assertEqual(self.call(self.lan + "/sched/resume", {"project": "exp"}, tok=admin)[0], 200)
        self.assertEqual(K.pause_by(), {})
        self.assertEqual(self.call(self.lan + "/sched/pause", {"project": "exp", "until": later}, tok=amy)[0], 200)
        self.assertEqual(K.pause_by(), {"exp": "amy"})
        self.assertEqual(self.call(self.lan + "/api/override/resume", {"target": "exp"}, tok=amy)[0], 200)
        self.assertNotIn("pause", K.sched())
        self.assertEqual(self.call(self.lan + "/api/override/pause", {"target": "exp"}, tok=amy)[0], 200)
        self.assertEqual(self.call(self.lan + "/sched/resume", {"project": "exp"}, tok=amy)[0], 200)
        K.set_sched_ovr("pause", "exp", True)                                   # 没记谁下的（手写、以前下的）：当管理员下的
        self.assertEqual(self.call(self.lan + "/sched/resume", {"project": "exp"}, tok=amy)[0], 403)
        snap = O.sched_view([{"id": "a", "status": {"t": 1, "share": {"weight": {}, "use": {}, "wait_cap_min": 30, "half_life_h": 24}}}],
                            [{"id": "exp"}, {"id": "other"}], time.time())
        self.assertEqual(snap["projects"][0]["paused"], {"until": None, "hosts": None, "by": None})
        K.set_sched_ovr("pause", "other", True, by="boss")
        snap = O.sched_view([{"id": "a", "status": {"t": 1, "share": {"weight": {}, "use": {}, "wait_cap_min": 30, "half_life_h": 24}}}],
                            [{"id": "exp"}, {"id": "other"}], time.time())
        self.assertEqual(snap["projects"][1]["paused"]["by"], "boss")
        seen = U.view(U.Ident("amy", "user", frozenset({"project"}), "token", "k"), {"sched": snap, "projects": [], "hosts": []})
        self.assertNotIn("by", seen["sched"]["projects"][1]["paused"])        # 别人项目的暂停：普通用户看不到是谁下的
        self.assertIn("by", seen["sched"]["projects"][0]["paused"])

    def test_host_pause_records_who_and_user_cannot_undo_admin_pause(self):
        """只给某几台的暂停（--host）也记谁下的（#27）：两台各按那台 worker 状态里的 by 判断，同 users.need_pause；
        普通用户在管理员下过的那台上撤不掉、也重下不了，另一台照常下，写的时候带 --by 与 --expect-by；管理员只带 --by。
        普通用户看得到的主机状态里，别人项目的指令去掉 by。"""
        amy = make_token("amy", role="user", scopes=("project",))
        admin = make_token("boss", role="admin", scopes=("admin", "project"))
        PJ.put_config("exp", 'id = "exp"\n', owner="amy")
        K.hosts_file().write_text('[[hosts]]\nid = "a"\nssh = "remote-a"\nusers = ["amy"]\n[[hosts]]\nid = "b"\nssh = "remote-b"\nusers = ["amy"]\n')
        ov = {"a": [{"kind": "pause", "target": "exp", "by": "boss"}, {"kind": "pause", "target": "other", "by": "bob"}],
              "b": [{"kind": "pause", "target": "exp", "once": True}]}
        save_json(state_dir() / "snapshot.json", {"hosts": [{"id": h, "status": {"t": time.time(), "overrides": o}} for h, o in ov.items()]})
        with mock.patch.object(S.W, "override_each", return_value=[{"host": "b", "rc": 0, "out": "ok"}]) as each:
            for path, data in (("/api/override/resume", {"target": "exp", "hosts": ["a"]}),
                               ("/api/override/pause", {"target": "exp", "hosts": ["b", "a"]}),
                               ("/api/override/pause", {"target": "exp", "hosts": ["a"], "now": True})):
                self.assertEqual(self.call(self.lan + path, data, tok=amy)[0], 403, path)
            each.assert_not_called()
            self.assertEqual(self.call(self.lan + "/api/override/pause", {"target": "exp", "hosts": ["b"]}, tok=amy)[0], 200)   # once 不算
            self.assertEqual(each.call_args.args[:4], ("add", "pause", "exp", ["b"]))
            self.assertEqual((each.call_args.kwargs["by"], each.call_args.kwargs["expect_by"]), ("amy", "amy"))
            self.assertEqual(self.call(self.lan + "/api/override/resume", {"target": "exp", "hosts": ["a"]}, tok=admin)[0], 200)
            self.assertEqual((each.call_args.kwargs["by"], each.call_args.kwargs["expect_by"]), ("boss", None))
        self.assertEqual(O.sched_view([{"id": h, "status": {"t": 1, "overrides": o, "share": {"weight": {}, "use": {}, "wait_cap_min": 30, "half_life_h": 24}}}
                                       for h, o in ov.items()], [{"id": "exp"}], time.time())["projects"][0]["paused"],
                         {"until": None, "hosts": ["a", "b"], "by": {"a": "boss"}})
        seen = U.view(U.Ident("amy", "user", frozenset({"project"}), "token", "k"), {"hosts": [{"id": h, "status": {"overrides": o}} for h, o in ov.items()]})
        self.assertEqual([o.get("by") for o in seen["hosts"][0]["status"]["overrides"]], ["boss", None])   # 自己项目的照给，别人项目的去掉

    def test_check_hides_hosts_not_opened_to_user(self):
        """普通用户的 labmon check（#16、#200）：默认打紧凑校验摘要，--probe 才打完整探针规格；
        引用了没开给他的主机，两种模式下都不泄露连接信息与探针规格；管理员带 probe=True 全打。"""
        amy = make_token("amy", role="user", scopes=("project",))
        admin = make_token("boss", role="admin", scopes=("admin", "project"))
        K.hosts_file().write_text('[[hosts]]\nid = "a"\nssh = "remote-a"\nusers = ["amy"]\n'
                                  '[[hosts]]\nid = "b"\nssh = "secret-b"\n[[hosts.checks]]\nid = "c"\ncmd = "secret-cmd"\n')
        text = 'id = "fresh"\n[[hosts]]\nid = "a"\n[[hosts]]\nid = "b"\n[[hosts]]\nid = "nowhere"\n'
        save_json(state_dir() / "snapshot.json", {"wake": []})
        # 默认紧凑摘要：不倾倒 probe_spec JSON，也不泄露未开放主机信息
        code, _, body = self.call(self.lan + "/api/project/fresh/check", {"text": text}, tok=amy)
        self.assertEqual(code, 200, body)
        out = json.loads(body)["out"]
        self.assertIn("校验通过：fresh（1 台主机：a，0 条清单）", out)
        self.assertIn("--probe", out)
        for leak in ("remote-a", "secret-b", "secret-cmd", '"worker_status"'):
            self.assertNotIn(leak, out)
        self.assertEqual(out.count("没开给你用"), 2)                          # 清单里有的 b 与没有的 nowhere 一个样
        # 带 probe=True：普通用户看开放主机的探针规格，仍隐藏未开放主机；管理员全看
        out_p = json.loads(self.call(self.lan + "/api/project/fresh/check", {"text": text, "probe": True}, tok=amy)[2])["out"]
        self.assertIn("remote-a", out_p)
        self.assertIn('"worker_status"', out_p)
        for leak in ("secret-b", "secret-cmd"):
            self.assertNotIn(leak, out_p)
        self.assertEqual(out_p.count("没开给你用"), 2)
        out_adm = json.loads(self.call(self.lan + "/api/project/fresh/check", {"text": text, "probe": True}, tok=admin)[2])["out"]
        self.assertIn("secret-b", out_adm)
        self.assertIn("secret-cmd", out_adm)

    def test_status_clips_running_last_line_and_check_probe_cli(self):
        """#200：api.status 对运行中任务的 last_line 限制单行 <= 100 字符并去换行；check --probe 透传参数。"""
        import contextlib
        import io
        from labmon import __main__ as M, api as A
        raw_tail = "Traceback (most recent call last):\n  " + "x" * 160
        snap = {"title": "演示", "generated": 1700000000.0, "hosts": [], "lanes": [{"id": "l1", "label": "车道 1", "host": "h1", "done": 0, "total": 1}],
                "jobs": [{"id": "j1", "lane": "l1", "status": "running", "elapsed_min": 5, "last_line": raw_tail, "log_mtime": 1699999990.0}],
                "groups": [], "alerts": []}
        out_dir = PJ.out_dir("clip")
        out_dir.mkdir(parents=True, exist_ok=True)
        save_json(out_dir / "snapshot.json", snap)
        res = A.status("clip", now=1700000010.0)
        log_lines = [ln for ln in res["out"].splitlines() if "日志" in ln]
        self.assertEqual(len(log_lines), 1, res["out"])
        shown = log_lines[0].split("：", 1)[1]
        self.assertNotIn("\n", shown)
        self.assertLessEqual(len(shown), 100)
        self.assertTrue(shown.endswith("…"), shown)
        self.assertEqual(res["data"]["jobs"][0]["last_line"], raw_tail)   # --json 原始数据不截断
        got = []
        with mock.patch.object(M, "cmd_check", lambda p, probe=False: got.append((p, probe)) or 0):
            M.main(["check", "-c", "m.toml"])
            M.main(["check", "-c", "m.toml", "--probe"])
        self.assertEqual(got, [("m.toml", False), ("m.toml", True)])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            M.main(["wait", "-c", "m.toml", "--probe"])

    def test_status_other_load_hint_and_user_isolation(self):
        """#197：api.status 在主机摘要行附极短他项聚合数（仅在有他项且 worker_up 时显示），普通用户经 users.view 同样安全不泄露他人项目名/任务名。"""
        from labmon import api as A, users as U
        now = 1700000000.0
        snap = {
            "title": "演示", "generated": now,
            "hosts": [
                {"id": "h-busy", "label": "h-busy", "ok": True, "gpus": [{"index": 0, "util": 20, "mem_used": 1024, "mem_total": 49152}]},
                {"id": "h-idle", "label": "h-idle", "ok": True, "gpus": [{"index": 0, "util": 0, "mem_used": 0, "mem_total": 49152}]},
                {"id": "h-down", "label": "h-down", "ok": True, "gpus": [{"index": 0, "util": 0, "mem_used": 0, "mem_total": 49152}]},
            ],
            "lanes": [], "jobs": [], "groups": [], "alerts": [],
        }
        out_dir = PJ.out_dir("mine")
        out_dir.mkdir(parents=True, exist_ok=True)
        save_json(out_dir / "snapshot.json", snap)
        busy_st = {
            "running": [
                {"project": "secret_proj", "name": "secret_r1", "root": "/x", "queue": "q.txt"},
                {"project": "secret_proj", "name": "secret_r2", "root": "/x", "queue": "q.txt"},
                {"project": "secret_proj", "name": "hold_skip", "hold": True, "root": "/x", "queue": "h.txt"},
                {"project": "mine", "name": "my_run", "root": "/m", "queue": "q.txt"},
            ],
            "todo": [
                {"project": "secret_proj", "root": "/x", "queue": "q.txt",
                 "jobs": ["secret_r1", "secret_r2"] + [f"secret_q{i}" for i in range(1, 9)], "held": None},
                {"project": "secret_proj", "root": "/p", "queue": "p.txt", "jobs": ["paused_x"], "held": "pause"},
            ],
            "plan": {"t": now, "lanes": [{"project": "secret_proj", "jobs": [{"name": "secret_q8", "t0": now, "t1": now + 3600}]}]},
        }
        glob = {
            "generated": now, "lan": {"ok": True},
            "projects": [{"id": "mine", "lanes": [{"host": "h-busy"}, {"host": "h-idle"}, {"host": "h-down"}]}],
            "hosts": [
                {"id": "h-busy", "worker_up": True, "status": busy_st},
                {"id": "h-idle", "worker_up": True, "status": {"running": [{"project": "mine", "name": "my_run"}], "todo": []}},
                {"id": "h-down", "worker_up": False, "status": busy_st},
            ],
        }
        # 管理员与普通用户（经 users.view）视角均只看到聚合数，绝不泄露他人明细
        acc = U.Access(projects={"mine"}, allowed={"h-busy", "h-idle", "h-down"}, hosts={"h-busy", "h-idle", "h-down"})
        who = U.Ident("amy", "user", frozenset({"read", "project"}), "token", "k")
        for g in (glob, U.view(who, glob, acc=acc)):
            res = A.status("mine", g, now=now + 10)
            lines = {ln.strip().split("：", 1)[0]: ln for ln in res["out"].splitlines() if "：" in ln}
            self.assertIn("；他项 2 跑/8 排", lines["h-busy"])
            self.assertNotIn("忙至", lines["h-busy"])
            self.assertNotIn("他项", lines["h-idle"])
            self.assertNotIn("他项", lines["h-down"])
            for secret in ("secret_proj", "secret_r1", "secret_r2", "secret_q1", "secret_q8", "paused_x", "hold_skip"):
                self.assertNotIn(secret, res["out"])

    def test_wait_request_cache_isolated_by_account_and_project(self):
        amy = make_token("amy", role="user", scopes=("project",))
        bob = make_token("bob", role="user", scopes=("project",))
        PJ.put_config("a", 'id = "a"\n', owner="amy")
        PJ.put_config("b", 'id = "b"\n', owner="bob")
        url = self.lan + "/api/wait"
        save_json(state_dir() / "snapshot.json", {"wake": []})
        a = self.call(url, {"project": "a", "rid": "same", "extra": {"demo": "甲"}}, tok=amy)
        b = self.call(url, {"project": "b", "rid": "same", "extra": {"demo": "乙"}}, tok=bob)
        self.assertEqual((a[0], b[0]), (200, 200), (a, b))
        self.assertEqual(json.loads(a[2])["new"]["demo"], "甲")
        self.assertEqual(json.loads(b[2])["new"]["demo"], "乙")

    def test_user_reads_own_global_schedule_records(self):
        amy = make_token("amy", role="user", scopes=("project",))
        PJ.put_config("own", 'id = "own"\n', owner="amy")
        PJ.put_config("other", 'id = "other"\n', owner="bob")
        self.srv.projects["own"] = Project("own")
        save_json(PJ.out_dir("own") / "snapshot.json", {"hosts": [{"id": "here"}]})
        K.set_sched_ovr("pause", "own", True)
        K.set_sched_ovr("pause", "own", False)
        until = time.time() + 60
        K.set_sched_ovr("focus", "own", True, until)
        K.expire_sched(until + 1)
        K.set_sched_ovr("pause", "other", True)
        log_events([{"kind": "override", "okind": "drain", "act": "add", "target": "own", "msg": "无关主机指令"}])
        for path in ("/records.json", "/p/own/events.json"):
            with self.subTest(path=path):
                status, _, payload = self.call(self.lan + path, tok=amy)
                self.assertEqual(status, 200, payload)
                result = json.loads(payload)
                rows = result["rows"] if path == "/records.json" else result["events"]["here"]
                self.assertCountEqual([(r["okind"], r["act"], r["target"]) for r in rows], [
                    ("pause", "add", "own"), ("pause", "rm", "own"), ("focus", "add", "own"), ("focus", "expire", "own"),
                ])

    def test_standby_precedes_object_permissions(self):
        amy = make_token("amy", role="user", scopes=("project",))
        read, _ = U.token_add("amy", "只读", ["read"])
        PJ.put_config("old", 'id = "old"\n', owner="bob")
        with mock.patch.object(S.LS, "primary", return_value=False):
            for method, path, body in (
                ("GET", "/api/project/old/config", None),
                ("HEAD", "/api/project/old/config", None),
                ("PUT", "/api/project/old/config", {"text": 'id = "old"\n'}),
                ("POST", "/api/wait", {"project": "old"}),
                ("POST", "/api/worker/status", {"host": "new-host"}),
            ):
                with self.subTest(method=method, path=path):
                    code, _, payload = self.call(self.lan + path, body, tok=amy, method=method)
                    self.assertEqual(code, 409, payload)
                    if method != "HEAD":
                        self.assertTrue(json.loads(payload)["standby"])
            self.assertEqual(self.call(self.lan + "/api/project/old/config")[0], 401)
            self.assertEqual(self.call(self.lan + "/api/wait", {"project": "old"}, tok=read)[0], 403)
            self.assertEqual(self.call(self.lan + "/p/old/")[0], 302)
            self.assertEqual(self.call(self.lan + "/p/old/", tok=amy)[0], 403)
            self.assertEqual(self.call(self.wan + "/login")[0], 200)
            self.assertEqual(self.call(self.wan + "/api/logout", {})[0], 200)

    def test_download_uses_running_commit_and_entry(self):
        tok = make_token("amy", role="user", scopes=("read",))
        with mock.patch.object(S.DI, "install_sh", return_value=("labmon-install.sh", "脚本")) as pack:
            path = "/api/client/install.sh?"
            for site, base in ((self.lan, self.lan + "/"), (self.wan, self.wan.replace("http:", "https:") + "/tx/")):
                url = site + path + urllib.parse.urlencode({"base": base})
                status, head, _ = self.call(url, tok=tok)
                self.assertEqual((status, head["Content-Disposition"]), (200, 'attachment; filename="labmon-install.sh"'))
                pack.assert_called_with(base, None, site == self.wan, S.DP.RUNNING["commit"])
            for base in (self.wan, "https://evil.example/tx/", self.wan.replace("http:", "https:") + "/../"):
                self.assertEqual(self.call(self.wan + path + urllib.parse.urlencode({"base": base}), tok=tok)[0], 400)
        with mock.patch.object(S.LS, "primary", return_value=False):
            status, _, response = self.call(self.wan + "/api/client/wheel", tok=tok)
            self.assertEqual(status, 409)
            self.assertTrue(json.loads(response)["standby"])

    def test_removed_account_does_not_inherit_old_grants(self):
        admin = make_token()
        amy = make_token("amy", role="user", scopes=("project",))
        PJ.put_config("exp", 'id = "exp"\n', owner="amy")
        K.hosts_file().write_text('[[hosts]]\nid = "a"\nssh = "remote-a"\nusers = ["amy"]\n')
        uri = self.lan + "/api/project/exp/config"
        self.assertEqual(self.call(uri, tok=amy)[0], 200)
        self.assertEqual(self.call(self.lan + "/api/users/remove", {"name": "Amy"}, tok=admin)[0], 200)
        self.assertEqual(self.call(uri, tok=amy)[0], 401)
        self.assertIsNone(PJ.owner("exp"))
        new = make_token("amy", role="user", scopes=("project",))
        self.assertEqual(self.call(uri, tok=new)[0], 403)
        self.assertEqual(json.loads(self.call(self.lan + "/api/hosts", tok=new)[2])["hosts"], [])

    def test_account_onboarding_binding(self):
        from labmon import fleet as F
        from labmon.state import save_json
        K.hosts_file().write_text('[fleet]\ninterval = 600\n')
        save_json(F.state_file("people.json"), {"Amy": {}, "Bob": {}})
        boss, ordinary = make_token(), make_token("reader", role="user", scopes=("project",))
        add = self.lan + "/api/users/add"
        listing = lambda: json.loads(self.call(self.lan + "/api/users", tok=boss)[2])  # noqa: E731
        self.assertEqual(listing()["person_choices"], ["Amy", "Bob"])
        for body in ({"name": "amy"}, {"name": "amy", "person": None}, {"name": "amy", "person": "未知"}):
            rc, _, raw = self.call(add, body, tok=boss)
            self.assertIn(rc, (400, 409))
            self.assertNotIn("code", json.loads(raw))
            self.assertIsNone(U.account("amy"))
        self.assertEqual(self.call(add, {"name": "amy", "person": "Amy"}, tok=ordinary)[0], 403)
        self.assertEqual(self.call(add, {"name": "amy", "person": "Amy"}, tok=boss)[0], 200)
        self.assertNotIn("Amy", listing()["person_choices"])
        self.assertEqual(self.call(add, {"name": "retry", "person": "Amy"}, tok=boss)[0], 409)
        self.assertIsNone(U.account("retry"))
        self.assertEqual(self.call(self.lan + "/fleet/op/person", {"name": "Amy", "remove": True}, tok=boss)[0], 409)
        self.assertEqual(self.call(add, {"name": "retry", "person": "Bob"}, tok=boss)[0], 200)

    def test_accounts_api(self):
        admin = make_token()
        from labmon import fleetboot as FB
        for name in ("amy", "carol"):
            FB.set_person(name, create=True, roster_only=True)
        code = json.loads(self.call(self.lan + "/api/users/add", {"name": "amy", "person": "amy"}, tok=admin)[2])["code"]
        self.assertRegex(code, r"^[2-9A-Z]{4}-[2-9A-Z]{4}-[2-9A-Z]{4}$")
        st, h, _ = self.call(self.lan + "/api/setup", {"name": "amy", "code": code, "password": "amy password"})
        amy = h["Set-Cookie"].split(";")[0].split("=", 1)[1]
        st, _, body = self.call(self.lan + "/api/password", {"old": "not it", "new": "amy password 2"}, cookie=amy)
        self.assertEqual((st, json.loads(body)["error"]), (403, "原口令不对"))       # 不是 401：页面拿到 401 会跳登录页（PR #176 审查）
        self.assertEqual(self.call(self.lan + "/api/me", cookie=amy)[0], 200)                              # 会话照样有效
        self.assertEqual(self.call(self.lan + "/api/users/add", {"name": "bob"}, cookie=amy)[0], 403)      # 普通用户不能加人
        tok = json.loads(self.call(self.lan + "/api/tokens/add", {"name": "只读", "scopes": ["read"]}, cookie=amy)[2])
        self.assertTrue(tok["token"].startswith("lm_"))
        self.assertEqual(self.call(self.lan + "/api/tokens/add", {"name": "x", "scopes": ["read"]}, tok=tok["token"])[0], 403)  # 令牌不能再造令牌
        self.assertEqual(self.call(self.lan + "/overview.json", tok=tok["token"])[0], 200)
        L = json.loads(self.call(self.lan + "/api/users", tok=admin)[2])
        self.assertEqual({u["name"]: (u["role"], len(u["sessions"]), len(u["tokens"])) for u in L["users"]}, {"boss": ("admin", 0, 1), "amy": ("user", 1, 1)})
        self.assertNotIn(tok["token"], json.dumps(L))
        self.assertEqual(self.call(self.lan + "/api/users/revoke", {"name": "amy"}, tok=admin)[0], 200)
        self.assertEqual(self.call(self.lan + "/overview.json", tok=tok["token"])[0], 401)               # 吊销后立即失效
        self.assertEqual(self.call(self.lan + "/overview.json", cookie=amy)[0], 401)
        self.call(self.lan + "/api/users/add", {"name": "carol", "person": "carol"}, tok=admin)
        st, _, body = self.call(self.lan + "/api/users/remove", {"name": "carol"}, tok=admin)
        self.assertEqual((st, json.loads(body)["msg"]), (200, "删了账号 carol，它的会话与令牌都已失效"))
        self.assertEqual(self.call(self.lan + "/api/users/remove", {"name": "boss"}, tok=admin)[0], 409)            # 不能删自己
        self.assertEqual(self.call(self.lan + "/api/users/remove", {"name": "amy"}, cookie=amy)[0], 401)
        self.assertNotIn("carol", [u["name"] for u in json.loads(self.call(self.lan + "/api/users", tok=admin)[2])["users"]])
        st, _, body = self.call(self.lan + "/api/users/role", {"name": "boss", "admin": False}, tok=admin)
        self.assertEqual((st, "最后一个管理员" in json.loads(body)["error"]), (409, True))
        self.assertEqual(self.call(self.lan + "/api/users/role", {"name": "amy", "admin": True}, tok=admin)[0], 200)
        self.assertEqual(self.call(self.lan + "/api/users/role", {"name": "boss", "admin": False}, tok=admin)[0], 200)
        self.assertEqual(self.call(self.lan + "/api/users", tok=admin)[0], 403)                          # 撤了管理员：它的 admin 令牌下一次请求就不行

    def test_wrong_password_not_logged(self):
        fixtures.add_account("boss", "admin", "本机命令行")
        lines = []
        with mock.patch("labmon.serve.log", lines.append):
            for _ in range(U.FAIL_MAX):
                self.assertEqual(self.call(self.lan + "/api/login", {"name": "boss", "password": "hunter2-secret"})[0], 401)
            self.assertEqual(self.call(self.lan + "/api/login", {"name": "boss", "password": "hunter2-secret"})[0], 429)
        self.assertTrue(lines)
        self.assertFalse([x for x in lines if "hunter2" in x])
        self.assertNotIn("hunter2", U.path().read_text())

    def test_routes_table(self):
        from labmon.serve import ROUTES, login_url, route_of, safe_next
        r = route_of("POST", "/something/new")
        self.assertEqual((r.scope, r.admin, r.lan), ("admin", True, False))      # 没登记的 POST 从严：只给管理员
        self.assertEqual(route_of("GET", "/whatever").scope, "read")
        for path in ("/fleet/op/renew", "/fleet/probe/x", "/renew/a/cancel"):
            self.assertTrue(route_of("POST", path).ops, path)
        self.assertEqual({r.path.pattern for r in ROUTES if r.method == "GET" and r.lan},
                         {r"/api/admin/backup", r"/api/users", r"/api/pools/options"})   # 只在内网入口的读：冷备、账号与算力池管理（#17、#240）
        self.assertEqual((login_url("/"), login_url("/p/a/"), login_url("/p/a")), ("login?next=", "../../login?next=p/a/", "../login?next=p/a"))
        for bad in ("//evil.example", "https://evil.example", "/abs", "../x", "a\\b", "p/../../x"):
            self.assertEqual(safe_next(bad), "", bad)
        self.assertEqual(safe_next("p/a/"), "p/a/")


class GuardTest(unittest.TestCase):
    """防跨站请求与 DNS 重绑定（#158，serve.Handler._gate）：POST 只认 JSON 类型、带了 Sec-Fetch-Site 的只认 same-origin 与 none；
    Host 只认 127.0.0.1、localhost 加这个入口的端口（[server] 另认的与缺省端口见 test_public.HostTest）。被拒的请求不进路由（也不看登录）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LABMON_STATE"] = str(Path(self.tmp.name) / "state")

    def tearDown(self):
        if getattr(self, "srv", None):
            self.srv.shutdown()
            self.srv.server_close()
        self.tmp.cleanup()

    def start(self):
        self.srv = Server(("127.0.0.1", 0), [])
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.tok = make_token()

    def req(self, method, path, headers=None, body=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest(method, path, skip_host="Host" in (headers or {}))
        for k, v in {"Authorization": f"Bearer {self.tok}", **(headers or {})}.items():
            c.putheader(k, v)
        b = body if body is not None else (b"{}" if method == "POST" else b"")
        if method == "POST":
            c.putheader("Content-Length", str(len(b)))
        c.endheaders(b or None)
        r = c.getresponse()
        out = (r.status, r.read())
        c.close()
        return out

    def test_post_needs_json_and_same_site(self):
        self.start()
        js = {"Content-Type": "application/json"}
        with mock.patch.object(self.srv.overview, "collect_now", return_value=None) as go:
            # 跨站表单能拼出的三种类型（text/plain 能凑出 JSON 请求体）、不带类型的都拒，不进路由
            for ct in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", None):
                code, _ = self.req("POST", "/collect?fresh", {"Content-Type": ct} if ct else {}, b'{"project": "a"}')
                self.assertEqual(code, 415, ct)
            for site in ("cross-site", "same-site"):                 # 别的站点（含同一站点的别的端口与子域）来的
                code, body = self.req("POST", "/collect?fresh", {**js, "Sec-Fetch-Site": site})
                self.assertEqual(code, 403, site)
                self.assertIn("跨站", json.loads(body)["error"])
            go.assert_not_called()
            # 同源页面（same-origin）、地址栏与命令行（none、不带）照常进路由；类型带 charset 也认
            for h in ({**js, "Sec-Fetch-Site": "same-origin"}, {**js, "Sec-Fetch-Site": "none"}, js,
                      {"Content-Type": "application/json; charset=utf-8"}):
                self.assertEqual(self.req("POST", "/collect?fresh", h)[0], 504, h)
            self.assertEqual(go.call_count, 4)
        self.assertEqual(self.req("POST", "/nope", js)[0], 404)

    def test_host_allowlist(self):
        self.start()
        js = {"Content-Type": "application/json"}
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"LocalHost:{self.port}"):
            self.assertEqual(self.req("GET", "/favicon.ico", {"Host": host})[0], 204, host)
            self.assertEqual(self.req("POST", "/nope", {**js, "Host": host})[0], 404, host)
        # DNS 重绑定：攻击者的域名解析到 127.0.0.1，Host 是它的名字；端口不对、不带端口、局域网 IP 都不认
        for host in (f"evil.example:{self.port}", f"127.0.0.1.evil.example:{self.port}", f"127.0.0.1:{self.port + 1}", "127.0.0.1",
                     f"192.0.2.3:{self.port}", f"localhost.:{self.port}"):
            code, body = self.req("GET", "/overview.json", {"Host": host})
            self.assertEqual(code, 403, host)
            self.assertIn("Host", body.decode())
            self.assertEqual(self.req("POST", "/nope", {**js, "Host": host})[0], 403, host)



class OverviewTest(unittest.TestCase):
    def test_reads_worker_status(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            wh = d / "alice" / "labmon" / "hosts" / "here"
            wh.mkdir(parents=True)
            now = time.time()
            j1 = running_job("j1", "demo-b", root=str(d / "alice" / "projects" / "demo-b" / "run"), pid=5, gpus=["GPU-a"], use={"ram": 30, "vram": 20000})
            st = fake_status(t=now, host="here", gpu_procs=[{"hpid": 9, "pid": 5, "mb": 20000, "uuid": "GPU-a", "owner": "job", "job": "j1"}],
                             info={"gpus": [{"i": 0, "uuid": "GPU-a", "name": "NVIDIA RTX A6000", "total": 49140, "used": 30000, "util": 80}],
                                   "mem": {"limit": 122, "used": 40, "src": "cgroup"}, "cpu_used": 3.5, "disk": [], "started": now - 3600},
                             running=[j1], waiting=[waiting_job("e", "a1", "等", root=str(d / "alice" / "project-a" / "r9"))])
            a1 = str(d / "alice" / "project-a")
            st["storage"] = {"projects": {a1: "a1"}, "local": {"a1": {"cache": 1, "scratch": 2}}}
            st["plan"] = {"t": now, "lanes": [{"root": str(d / "alice" / "projects" / "demo-b" / "run"), "queue": "jobs/q.txt", "project": "demo-b", "kind": "queue",
                                               "jobs": [{"name": "j1", "t0": now - 3600, "t1": now + 600, "gpus": [0], "run": True},
                                                        {"name": "j2", "t0": now + 600, "t1": now + 1200, "gpus": [0], "after": [["a1", "x"]]}]}]}
            st["info"]["disk"] = [{"path": str(d / "alice"), "free": 200, "total": 1000}]
            (wh / "status.json").write_text(json.dumps(st))
            ix = d / "alice" / "labmon" / "disk"
            ix.mkdir(parents=True)
            (ix / "index.json").write_text(json.dumps({"root": str(d), "host": "here", "t": now, "split": [], "units": {
                a1: {"b": 5 * 2 ** 30, "n": 9, "t": now, "s": 3, "age": [0, 0, 0, 0, 0]},
                str(d / "alice" / "other"): {"b": 1, "n": 1, "t": now, "s": 1, "age": [0, 0, 0, 0, 0]}}}))
            (wh / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
                {"t": now - 60, "kind": "start", "msg": "开始 j1"},
                {"t": now - 50, "kind": "override", "msg": "临时指令：drain 主机，无期限", "act": "add", "okind": "drain"},
                {"t": now - 40, "kind": "wait", "msg": "等待", "root": str(d / "alice" / "project-a" / "r9"), "queue": "jobs/q.txt", "why": "drain"}]))
            (wh / "worker.log").write_text("2026-11-04 10:00:00 worker 启动\n")
            # old 没写 project、没有起止（存用量时跳过）；old2、old3 是 epoch 秒的 t0 / t1
            (wh / "usage.jsonl").write_text(json.dumps({"name": "old", "root": str(d / "alice" / "project-a" / "r9"), "rc": "0"}) + "\n"
                                            + json.dumps({"name": "old2", "root": str(d / "alice" / "project-a" / "r9"), "project": "a1", "rc": "0",
                                                          "t0": now - 7200, "t1": now - 3600, "gpus": ["GPU-a"]}) + "\n"
                                            + json.dumps({"name": "old3", "root": str(d / "alice" / "project-a" / "r9"), "project": "a1", "rc": "0",
                                                          "t0": now - 3000, "t1": now - 2400, "gpus": ["GPU-a"], "wait": 5.0}) + "\n")
            (d / "hosts.toml").write_text(f'[[hosts]]\nid = "here"\ndata_root = "{d / "alice"}"\n'
                                          f'[[hosts]]\nid = "gone"\nreturned = "2026-11-01 10:00"\n'
                                          '[sched]\nhalf_life_h = 12\nwait_cap_min = 0\n[sched.weight]\na1 = 2\ndemo-b = "x"\n')
            env = {"LABMON_HOSTS": str(d / "hosts.toml"), "LABMON_STATE": str(d / "state")}
            old = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                pj = [{"id": "a1", "title": "A1", "queues": [{"root": str(d / "alice" / "project-a" / "*"), "host": "here"}]},
                      {"id": "demo-b", "running": [{"id": "j1", "progress": [3, 10], "eta_ts": now + 600}]}]
                snap = O.collect(pj)
                snap = O.collect(pj)
            finally:
                for k, v in old.items():
                    os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
            self.assertEqual([h["id"] for h in snap["hosts"]], ["here"])          # 已归还的主机不读
            h = snap["hosts"][0]
            self.assertTrue(h["worker_up"])
            self.assertEqual((h["status"]["running"][0]["project"], h["status"]["running"][0]["link"]), ("demo-b", "demo-b"))   # 只认 worker 写的项目 id
            self.assertEqual((h["status"]["waiting"][0]["project"], h["status"]["waiting"][0]["link"]), ("a1", "a1"))
            self.assertEqual([x.get("project") for x in h["done"]], [None, "a1", "a1"])     # 没写 project 的旧行不按目录猜
            self.assertEqual(h["log"], ["2026-11-04 10:00:00 worker 启动"])
            self.assertEqual(len(h["series"]), 2)
            self.assertEqual(h["status"]["running"][0]["prog"]["progress"], [3, 10])   # 进度按项目与任务名从项目看板对上
            self.assertEqual(set(snap["colors"]), {"demo-b", "a1"})
            self.assertIn('"worker_up": true', (d / "state" / "overview" / "index.html").read_text())
            # 运行中任务的预计结束（项目看板按进度算的）写给 worker 推演；占用：已跑的从本机存的用量记录来，在跑与排队的从推演来
            self.assertEqual(json.loads((wh / "eta.json").read_text())["jobs"], {"demo-b|j1": round((now + 600) / 60) * 60})
            self.assertEqual(json.loads((wh / "eta.json").read_text())["prog"], [])            # 未标进度来源的不进 prog（#149）
            usage = load_rows(d / "state" / "overview" / "usage" / "here.jsonl")
            self.assertEqual((usage[0]["gi"], usage[0]["wait"]), ([0], None))                  # 用的卡也存序号（换容器后 uuid 全变还能画）
            self.assertEqual([(r["n"], r["s"], r["t"]) for r in usage], [("old2", now - 7200, now - 3600), ("old3", now - 3000, now - 2400)])
            os.environ.update(env)
            try:                                                     # eta.json 的 t 是给 worker 与服务器上的任务开始时刻比的：按这台的时钟差换算
                O._CLOCK["here"] = 3600
                pj[1]["running"][0]["eta_ts"] = now + 1200
                O.collect(pj)
            finally:
                for k, v in old.items():
                    os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
            self.assertGreater(json.loads((wh / "eta.json").read_text())["t"], now + 3000)
            sj = json.loads((wh / "sched.json").read_text())
            own = sj.pop("owner")
            self.assertEqual(sj, {"half_life_h": 12, "weight": {"a1": 2}})   # [sched] 原样下发，不是正数的不发
            self.assertEqual((set(own), len(set(own.values()))), ({"a1", "demo-b"}, 1))   # 各项目算在谁名下（#23）：都没主人，归管理员、算一个人
            self.assertEqual(json.loads((wh / "returned.json").read_text()), {"hosts": ["gone"]})   # 主机清单里已归还的（#25 非阻塞 1）
            occ = {r["n"]: r for r in snap["occ"]["here"]}
            self.assertEqual((occ["old2"]["st"], occ["old2"]["g"], occ["old2"]["p"]), ("done", [0], "a1"))
            self.assertEqual((occ["j1"]["st"], occ["j2"]["st"], occ["j2"]["after"]), ("run", "plan", [["a1", "x"]]))
            self.assertEqual(len(load_rows(d / "state" / "overview" / "usage" / "here.jsonl")), 2)   # 两次采集不重复记
            # worker 事件续存进本机 hostlog（跳过任务起止，两次采集不重复），项目页按主机取 30 天
            os.environ["LABMON_STATE"] = str(d / "state")
            try:
                self.assertEqual([e["kind"] for e in records(host="here")][::-1], ["override", "wait"])
                from labmon import disk as DK
                st = DK.project_storage("a1")
                self.assertEqual([(d["path"], d["name"], d["b"], d["t"]) for d in st["dirs"]], [(a1, "", 5 * 2 ** 30, now)])   # 旧 worker 没拆开：整个项目目录一项
                self.assertEqual((st["roots"], st["b"], st["free"], st["total"], st["warn_gb"], st["local"]),
                                 ([a1], 5 * 2 ** 30, 200 * 2 ** 30, 1000 * 2 ** 30, 30, {"here": {"cache": 1, "scratch": 2}}))
                self.assertEqual([o["name"] for o in snap["disk"]["owners"]], ["alice"])     # 索引变了才取回（第二次采集不再传）
            finally:
                os.environ.pop("LABMON_STATE") if old["LABMON_STATE"] is None else os.environ.__setitem__("LABMON_STATE", old["LABMON_STATE"])


class RegsTest(unittest.TestCase):
    """登记的全局视图（全局快照的 regs，#93）：在用主机取状态里的 queues，已归还的与主机清单里没有的经同一 worker_home 的第一台在用主机读登记文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = Path(self.tmp.name)
        self.home = d / "alice" / "labmon" / "hosts"
        (d / "hosts.toml").write_text(f'[[hosts]]\nid = "a"\ndata_root = "{d / "alice"}"\n[[hosts]]\nid = "b"\ndata_root = "{d / "alice"}"\n'
                                      f'[[hosts]]\nid = "c"\ndata_root = "{d / "other"}"\n'
                                      f'[[hosts]]\nid = "gone"\nlabel = "借 X"\ndata_root = "{d / "alice"}"\nreturned = "2026-11-01 10:00"\n')
        self.env = {"LABMON_HOSTS": str(d / "hosts.toml"), "LABMON_STATE": str(d / "state")}
        self.old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        O._SENT.clear()

    def tearDown(self):
        for k, v in self.old.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def reg(self, host, name, **kw):
        q = self.home / host / "queues"
        q.mkdir(parents=True, exist_ok=True)
        (q / f"{name}.json").write_text(kw.pop("raw", None) or json.dumps(kw))

    def test_active_status_returned_files_and_stale(self):
        st_q = {"root": "/r/p1", "jobs": "jobs/a.txt", "gpus": [0], "project": "p1", "stop": "boundary", "empty": True, "dur": {}}
        (self.home / "a").mkdir(parents=True)
        (self.home / "a" / "status.json").write_text(json.dumps(fake_status(host="a", queues=[st_q])))
        self.reg("a", "only_in_file", root="/r/x", jobs="jobs/x.txt", project="p9")          # a 有状态：登记取状态，不读文件
        self.reg("b", "b1", root="/r/p1", jobs="jobs/b.txt", project="p1", added="2026-11-10 09:00")   # b 的 worker 没写过状态：读文件
        self.reg("gone", "g1", root="/r/p1", jobs="jobs/g.txt", project="p1", stop="term")
        self.reg("gone", "bad", raw="{半个")
        self.reg("old", "o1", root="/r/p2", jobs="jobs/o.txt", project="p2")                   # 主机清单里已没有这台
        (self.home / "empty" / "queues").mkdir(parents=True)
        calls = []
        real = O.fetch
        O.fetch = lambda h, timeout=60: (calls.append((h["id"], h.get("regs"))), real(h, timeout))[1]
        try:
            snap = O.collect([], use_ssh=False)
        finally:
            O.fetch = real
        regs = {x["host"]: x for x in snap["regs"]}
        self.assertEqual([x["host"] for x in snap["regs"]], ["a", "b", "c", "gone", "empty", "old"])
        self.assertEqual(dict(calls)["a"], ["a", "b", "c"])                 # 每个 worker_home 挑第一台：a 读 alice 的，c 读 other 的
        self.assertEqual(dict(calls)["c"], ["a", "b", "c"])
        self.assertIsNone(dict(calls)["b"])
        a = regs["a"]
        self.assertEqual((a["state"], a["stale"], a["queues"]),
                         ("active", False, [{"root": "/r/p1", "jobs": "jobs/a.txt", "project": "p1", "stop": "boundary", "gpus": [0], "empty": True, "sleeping": False, "added_t": None}]))
        self.assertEqual([(q["jobs"], q["added"], q["key"]) for q in regs["b"]["queues"]], [("jobs/b.txt", "2026-11-10 09:00", "b1")])
        self.assertEqual(regs["c"]["queues"], [])                          # c 的 worker_home 读到了、没有登记
        g = regs["gone"]
        self.assertEqual((g["state"], g["label"], g["returned"], [q["stop"] for q in g["queues"]]),
                         ("returned", "借 X", K.parse_time("2026-11-01 10:00"), ["term"]))
        self.assertEqual((regs["old"]["state"], regs["old"]["queues"][0]["project"]), ("unlisted", "p2"))
        self.assertEqual(regs["empty"]["queues"], [])
        # 首选代读主机 a 这一轮连不上、同域 b 仍在线：自动切换到 b 补读登记（#212），a 自己沿用并标 stale，b 与 gone 均读到最新（不标 stale）
        O.fetch = lambda h, timeout=60: (None, "ssh 超时") if h["id"] == "a" else real(h, timeout)
        try:
            snap = O.collect([], use_ssh=False)
        finally:
            O.fetch = real
        regs = {x["host"]: x for x in snap["regs"]}
        self.assertEqual((regs["gone"]["stale"], regs["gone"]["queues"][0]["jobs"]), (False, "jobs/g.txt"))
        self.assertEqual((regs["a"]["stale"], regs["a"]["queues"][0]["jobs"]), (True, "jobs/a.txt"))
        self.assertEqual((regs["b"]["stale"], regs["b"]["queues"][0]["jobs"]), (False, "jobs/b.txt"))
        self.assertFalse(regs["c"]["stale"])
        self.assertEqual({x["host"] for x in snap["regs"]}, {"a", "b", "c", "gone", "empty", "old"})
        # 整块共享域（a 与 b）都连不上：已归还的与该域主机均沿用上一轮、标 stale，另一域 c 不受影响
        O.fetch = lambda h, timeout=60: (None, "ssh 超时") if h["id"] in ("a", "b") else real(h, timeout)
        try:
            snap = O.collect([], use_ssh=False)
        finally:
            O.fetch = real
        regs = {x["host"]: x for x in snap["regs"]}
        self.assertEqual((regs["gone"]["stale"], regs["gone"]["queues"][0]["jobs"]), (True, "jobs/g.txt"))
        self.assertEqual((regs["a"]["stale"], regs["a"]["queues"][0]["jobs"]), (True, "jobs/a.txt"))
        self.assertEqual((regs["b"]["stale"], regs["b"]["queues"][0]["jobs"]), (True, "jobs/b.txt"))
        self.assertFalse(regs["c"]["stale"])
        self.assertEqual({x["host"] for x in snap["regs"]}, {"a", "b", "c", "gone", "empty", "old"})

    def test_project_lane_follows_registration(self):
        # 端到端（#94）：几台共用一个 worker_home；monitor.toml 不写 [[queues]] 与 [[hosts]]，工作台读到的登记（regs）在 b 上 → 项目页的车道在 b；
        # 登记换到 a（worker add 搬过去）→ 同一车道 id 换到 a，b 不再是项目的主机；别的项目的登记不进来
        root = self.d / "proj"
        (root / "jobs").mkdir(parents=True)
        (root / "jobs" / "q.txt").write_text("x1|echo 1\nx2|echo 2\n")
        (self.d / "m.toml").write_text('id = "p1"\n')
        cfg = load(self.d / "m.toml")
        self.reg("b", "q", root=str(root), jobs="jobs/q.txt", project="p1")
        self.reg("b", "z", root="/r/z", jobs="jobs/z.txt", project="p9")
        O.collect([], use_ssh=False)
        snap = C.collect(cfg)
        self.assertEqual(([h["id"] for h in snap["hosts"]], [(ln["id"], ln["host"], ln["total"]) for ln in snap["lanes"]]), (["b"], [("proj/jobs/q.txt", "b", 2)]))
        (self.home / "b" / "queues" / "q.json").unlink()
        self.reg("a", "q", root=str(root), jobs="jobs/q.txt", project="p1")
        O.collect([], use_ssh=False)
        snap = C.collect(cfg)
        self.assertEqual(([h["id"] for h in snap["hosts"]], [(ln["id"], ln["host"], ln["total"]) for ln in snap["lanes"]]), (["a"], [("proj/jobs/q.txt", "a", 2)]))


    def test_duplicate_on_two_active_hosts_takes_later(self):
        # 同一清单登记在两台在用主机上（#88 之前会有）：在用主机的登记取自 worker 状态，状态里带登记时刻 added，项目车道取登记晚的那台（Codex 审查 #114）
        for h, added in (("a", 1_800_000_600), ("b", 1_800_000_000)):
            (self.home / h).mkdir(parents=True)
            q = {"root": "/r/p", "jobs": "jobs/q.txt", "gpus": None, "project": "p1", "stop": "boundary", "empty": False, "added_t": added, "dur": {}}
            (self.home / h / "status.json").write_text(json.dumps(fake_status(host=h, queues=[q])))
        regs = O.collect([], use_ssh=False)["regs"]
        self.assertEqual({x["host"]: x["queues"][0]["added_t"] for x in regs if x["host"] in ("a", "b")}, {"a": 1_800_000_600, "b": 1_800_000_000})
        (self.d / "m.toml").write_text(f'id = "p1"\nout_dir = "{self.d / "out"}"\n')
        qs, _ = C.reg_queues(load(self.d / "m.toml"), regs[::-1], {}, time.time())
        self.assertEqual([(q["id"], q["host"]) for q in qs], [("p/jobs/q.txt", "a")])

    def _worker_home(self):
        """各主机共用的 worker_home 里放上这份 worker（没有 ssh 的主机在本机跑 worker add / rm），再建一条清单。"""
        from fixtures import copy_worker
        self.home.mkdir(parents=True, exist_ok=True)
        copy_worker(self.home.parent)
        root = self.d / "proj"
        (root / "jobs").mkdir(parents=True)
        (root / "jobs" / "q.txt").write_text("x1|echo 1\n")
        return str(root)

    def test_move_and_remove_from_page(self):
        # #95：页面上搬 = 在目标主机上 worker add（把别的主机上同一条清单的登记删掉）：带 project、stop，不带原主机的 --gpus；
        # 已归还主机上的登记经 worker_home 相同的在用主机代办移除（命令照样 --host 那台，不连它）；每条记一条配置改动。
        # #121：按主机成批（rm 带 items），做成的按项目告诉实验会话一次（records.tell → wake 的 page:）
        from labmon import projects, serve as S
        projects.put_config("p1", 'id = "p1"\n')
        root = self._worker_home()
        self.reg("gone", "q", root=root, jobs="jobs/q.txt", project="p1", stop="term", gpus=[1], added="2026-11-01 09:00:00")
        rq = lambda: {h.name: [json.loads(f.read_text()) for f in (h / "queues").glob("*.json")] for h in self.home.iterdir() if (h / "queues").is_dir()}   # noqa: E731
        msg = S.reg_move("a", [{"host": "gone", "root": root, "jobs": "jobs/q.txt", "project": "p1", "stop": "term"}])
        self.assertIn("已登记", msg)
        regs = rq()
        self.assertEqual(regs.get("gone"), [])                                       # 原主机那份由 worker add 删掉
        self.assertEqual([(r["project"], r["stop"], r.get("gpus")) for r in regs["a"]], [("p1", "term", None)])
        self.assertEqual([(e["kind"], e["hosts"], e["project"]) for e in [e for e in records(cat="config") if e.get("kind") == "config"]], [("config", ["a", "gone"], "p1")])
        w = told()
        self.assertEqual([(x["key"].split(":")[:3], x["projects"]) for x in w], [(["page", "move", "p1"], ["p1"])])
        self.assertIn("把 p1 在 gone 上的 1 条清单登记搬到 a（jobs/q.txt）", w[0]["msg"])
        self.assertIn("labmon worker add a ROOT JOBS --project p1", w[0]["msg"])
        # 在跑的不让移除：按全局快照里这台的 running
        save_json(state_dir() / "snapshot.json", {"hosts": [fake_host("a", status=fake_status(host="a", running=[running_job("x1", "p1", root=root, queue="jobs/q.txt")]))]})
        with self.assertRaises(K.ConfigError) as e:                                    # 一批里有一条在跑：整批拒绝，一条也不移除
            S.reg_rm("a", [{"root": root, "jobs": "jobs/none.txt"}, {"root": root, "jobs": "jobs/q.txt"}], project="p1")
        self.assertIn("x1 正在跑", str(e.exception))
        self.assertEqual((len(rq()["a"]), len(told())), (1, 1))
        save_json(state_dir() / "snapshot.json", {"hosts": []})
        # 已归还主机上的登记：经 a 代办（ssh 只叫到 a，--host 是 gone）
        self.reg("gone", "q", root=root, jobs="jobs/q.txt", project="p1")
        calls, real = [], S.W.ssh
        with mock.patch.object(S.W, "ssh", lambda h, remote, stdin="": (calls.append((h["id"], remote)), real(h, remote, stdin))[1]):
            self.assertIn("移除了 gone 上的 1 条登记", S.reg_rm("gone", [{"root": root, "jobs": "jobs/q.txt"}], project="p1"))
            with self.assertRaises(K.ConfigError):                                       # 主机清单里没有、又没给 worker_home 的：说不出该经哪台
                S.reg_rm("old", [{"root": root, "jobs": "jobs/q.txt"}])
            with self.assertRaises(K.ConfigError) as e:                                # 页面从 regs 带上它的 worker_home；已经没有这条了：报回、不记
                S.reg_rm("old", [{"root": root, "jobs": "jobs/q.txt"}], home=str(self.d / "alice" / "labmon"), project="p1")
            self.assertIn("没登记过", str(e.exception))
        w = told()
        self.assertEqual([x["key"].split(":")[1] for x in w], ["move", "rm"])           # 没移除成的不告诉
        self.assertIn("移除了 p1 在 gone 上的 1 条清单登记（jobs/q.txt）", w[1]["msg"])
        with self.assertRaises(K.ConfigError):
            S.reg_rm("a", [])
        self.assertEqual([h for h, _ in calls], ["a", "a"])
        self.assertTrue(all("--host gone" in r or "--host old" in r for _, r in calls))
        self.assertEqual(rq()["gone"], [])
        # 不认识的主机、worker add 报错（清单不在）：原样带回，不记录
        with self.assertRaises(K.ConfigError) as e:
            S.reg_move("nope", [{"host": "a", "root": root, "jobs": "jobs/q.txt", "project": "p1"}])
        self.assertIn("没有主机 nope", str(e.exception))
        with self.assertRaises(K.ConfigError) as e:
            S.reg_move("b", [{"host": "a", "root": root, "jobs": "jobs/none.txt", "project": "p1"}])
        self.assertIn("没搬到 b：清单不存在", str(e.exception))
        self.assertEqual((len([e for e in records(cat="config") if e.get("kind") == "config"]), len(told())), (2, 2))

    def test_home_peers_in_host_list_and_worker_status(self):
        # 共用 worker 目录（同一块共享盘）的在用主机（#35 A）：a、b 同目录，c 另一块盘，已归还的 gone 不算；
        # /api/hosts 每台给 worker_home 与 home_with，worker status 末尾补一行，普通用户只列他能用的
        from labmon import api as A
        from labmon import workerctl as W
        self.assertEqual((K.home_peers("a"), K.home_peers("c"), K.home_peers("gone")), (["a", "b"], ["c"], []))
        hs = {h["id"]: h for h in A.hosts()["hosts"]}
        self.assertEqual((hs["a"]["home_with"], hs["b"]["home_with"], hs["c"]["home_with"], hs["gone"]["home_with"]), (["b"], ["a"], [], []))
        self.assertEqual(hs["a"]["worker_home"], str(self.d / "alice" / "labmon"))
        with mock.patch.object(W, "run_out", return_value=(0, "worker：运行中 PID 1\n")):
            out = A.worker("status", "a", [])["out"]
            self.assertIn(f"与 b 共用 worker 目录 {self.d / 'alice' / 'labmon'}", out)
            self.assertNotIn("共用", A.worker("status", "a", [], may=lambda h: h == "a")["out"])
            self.assertNotIn("共用", A.worker("status", "c", [])["out"])
        with mock.patch.object(W, "run_out", return_value=(3, "连不上\n")):
            self.assertNotIn("共用", A.worker("status", "a", [])["out"])
        # fit --list（#35 B）：看板给 worker 共用 worker 目录的在用主机（普通用户只给他能用的；这台自己总在）
        with mock.patch.object(W, "run_out", return_value=(0, "")) as ro:
            A.worker("fit", "a", ["/r", "jobs/q.txt"], project="p", lst=True)
            A.worker("fit", "a", ["/r", "jobs/q.txt"], project="p", lst=True, may=lambda h: False)
            A.worker("fit", "a", ["x_s1"], project="p")
        self.assertEqual([(c.kwargs.get("list"), c.kwargs.get("hosts")) for c in ro.call_args_list], [(True, "a,b"), (True, "a"), (None, None)])
        with self.assertRaises(U.Denied):
            S._worker_args("fit", {"args": ["/r", "jobs/q.txt"], "list": True})          # --list 要 project
        with self.assertRaises(U.Denied):
            S._worker_args("status", {"args": [], "list": True, "project": "p"})
        self.assertEqual(S._worker_args("fit", {"args": ["/r", "jobs/q.txt"], "list": True, "project": "p"}), ["/r", "jobs/q.txt"])
        import contextlib
        import io
        from labmon import __main__ as M
        with mock.patch.object(M, "_call", return_value=A.hosts()), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main(["host", "list"]), 0)
        self.assertEqual([x for x in out.getvalue().splitlines() if "共用" in x],
                         [f"  共用 worker 目录 {self.d / 'alice' / 'labmon'}（同一块共享盘，放在这块盘上的文件各台都读得到）：a、b"])

    def test_prev_host_and_home_host(self):
        # 在用主机那项带 pool 与上一台 prev：同一个池里最近归还的自用主机（fleetboot.last_mine，给自己开回来沿用端口的那台，接上卡排第一）；
        # 别的池、没写 platform_job 的不算。代读、代办的那台是同一 worker_home 里照主机清单顺序的第一台在用主机（config.home_host）
        from labmon import fleetboot as FB
        x = self.d / "alice"
        hf = Path(self.env["LABMON_HOSTS"])
        hf.write_text(f'[[hosts]]\nid = "n-2"\ndata_root = "{x}"\nplatform_job = "ME"\nplatform_pool = "P"\nport = 2\n'
                      f'[[hosts]]\nid = "o1"\ndata_root = "{x}"\nplatform_job = "ME"\nplatform_pool = "P"\nport = 1\nreturned = "2026-11-01 10:00"\n'
                      f'[[hosts]]\nid = "o2"\ndata_root = "{x}"\nplatform_job = "ME"\nplatform_pool = "P"\nport = 3\nreturned = "2026-11-05 10:00"\n'
                      f'[[hosts]]\nid = "o3"\ndata_root = "{x}"\nplatform_job = "ME"\nplatform_pool = "Q"\nport = 4\nreturned = "2026-11-09 10:00"\n'
                      f'[[hosts]]\nid = "g"\ndata_root = "{x}"\nport = 5\nreturned = "2026-11-09 11:00"\n')
        self.assertEqual((FB.last_mine("P")["id"], FB.last_mine("P", skip="o2")["id"], FB.last_mine(None)), ("o2", "o1", None))
        self.assertEqual(K.home_host(str(x / "labmon") + "/")["id"], "n-2")
        self.assertIsNone(K.home_host("/别处"))
        regs = {r["host"]: r for r in O.collect([], use_ssh=False)["regs"]}
        self.assertEqual((regs["n-2"]["pool"], regs["n-2"]["prev"]), ("P", "o2"))

    def test_reg_route_local_and_offline(self):
        # POST /reg/…：要登录（project 作用域），本机不在内网时拒绝（同调度卡）
        srv = Server(("127.0.0.1", 0), [])
        login(self)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            post = lambda act, b: urllib.request.urlopen(post_req(f"{base}/reg/{act}", json.dumps(b).encode()))  # noqa: E731
            with mock.patch.dict(os.environ, {"LABMON_LAN": "off"}), self.assertRaises(urllib.error.HTTPError) as e:
                post("rm", {"host": "a", "project": "p1", "items": [{"root": "/r", "jobs": "q.txt"}]})
            self.assertEqual((e.exception.code, "不在内网" in json.loads(e.exception.read())["error"]), (409, True))
            with self.assertRaises(urllib.error.HTTPError) as e:
                post("nope", {})
            self.assertEqual(e.exception.code, 404)
        finally:
            srv.shutdown()
            srv.server_close()

    def _two(self):
        """a、b 两台在用、同一 worker_home，都有状态（各登记一条）。"""
        for h in ("a", "b"):
            (self.home / h).mkdir(parents=True)
            q = {"root": f"/r/{h}", "jobs": "jobs/q.txt", "gpus": None, "project": "p1", "stop": "boundary", "empty": False, "dur": {}}
            (self.home / h / "status.json").write_text(json.dumps(fake_status(host=h, queues=[q])))
            self.reg(h, "q", root=f"/r/{h}", jobs="jobs/q.txt", project="p1")
        self.assertEqual({x["host"]: len(x["queues"]) for x in O.collect([], use_ssh=False)["regs"] if x["queues"] is not None}, {"a": 1, "b": 1, "c": 0})

    def test_failed_host_with_status_keeps_last_round(self):
        # b 有 status.json（代读的 a 不扫它），这一轮连不上：沿用上一轮、标 stale，不能当成“读到了、没有登记”（Codex 审查 #105）
        self._two()
        real = O.fetch
        O.fetch = lambda h, timeout=60: (None, "ssh 超时") if h["id"] == "b" else real(h, timeout)
        try:
            regs = {x["host"]: x for x in O.collect([], use_ssh=False)["regs"]}
        finally:
            O.fetch = real
        self.assertEqual((regs["b"]["stale"], [q["root"] for q in regs["b"]["queues"]]), (True, ["/r/b"]))
        self.assertFalse(regs["a"]["stale"])

    def test_just_returned_host_kept_when_not_read(self):
        # b 上一轮在用、这一轮归还了，代读的 a 又连不上：b 的登记沿用上一轮、标 stale，状态改成已归还（Codex 审查 #105）
        self._two()
        hf = Path(self.env["LABMON_HOSTS"])
        hf.write_text(hf.read_text().replace(f'id = "b"\ndata_root = "{self.d / "alice"}"\n', f'id = "b"\ndata_root = "{self.d / "alice"}"\nreturned = "2026-11-09 10:00"\n'))
        real = O.fetch
        O.fetch = lambda h, timeout=60: (None, "ssh 超时") if h["id"] == "a" else real(h, timeout)
        try:
            regs = {x["host"]: x for x in O.collect([], use_ssh=False)["regs"]}
        finally:
            O.fetch = real
        b = regs["b"]
        self.assertEqual((b["state"], b["returned"], b["stale"], [q["root"] for q in b["queues"]]), ("returned", K.parse_time("2026-11-09 10:00"), True, ["/r/b"]))
        regs = {x["host"]: x for x in O.collect([], use_ssh=False)["regs"]}          # a 连上了：b 的目录由 a 读回（不再跳过）
        self.assertEqual((regs["b"]["state"], regs["b"]["stale"], [q["root"] for q in regs["b"]["queues"]]), ("returned", False, ["/r/b"]))


class WakeMovedTest(unittest.TestCase):
    def test_wakes_projects_whose_regs_changed(self):
        # #78：全局采集一轮后登记变了的项目（算力池放到某台、搬、登记、撤）当场叫醒它的采集线程；没变的、第一轮都不叫
        class P:
            def __init__(self, pid):
                self.id, self.wake = pid, threading.Event()
        pa, pb = P("pa"), P("pb")
        ov = Overview([pa, pb])
        q = lambda p, j: {"root": "/r", "jobs": j, "project": p}          # noqa: E731
        s1 = {"regs": [{"host": "pool", "state": "pool", "queues": [q("pa", "x.txt")]}, {"host": "h", "state": "active", "queues": [q("pb", "y.txt")]}]}
        self.assertEqual(ov.wake_moved(s1), [])
        self.assertEqual(ov.wake_moved(s1), [])
        s2 = {"regs": [{"host": "pool", "state": "pool", "queues": []}, {"host": "h", "state": "active", "queues": [q("pb", "y.txt"), q("pa", "x.txt")]}]}
        self.assertEqual(ov.wake_moved(s2), ["pa"])
        self.assertTrue(pa.wake.is_set())
        self.assertFalse(pb.wake.is_set())
        s3 = {"regs": [{"host": "pool", "state": "pool", "queues": []}, {"host": "h", "state": "active", "queues": [q("pa", "x.txt")]},
                       {"host": "old", "state": "unlisted", "queues": [q("pb", "y.txt")]}]}
        self.assertEqual(ov.wake_moved(s3), ["pb"])                      # pb 撤掉了（主机清单里没有的那台不算）


class LongTermTest(unittest.TestCase):
    def test_spans_and_hourly_util(self):
        # 采集区间：隔得近的接上，断开超过 10 分钟另起一段；每小时 GPU 利用率按次平均，取各次的吃满程度 busy（#51），
        # 只有探针瞬时值 util 的那次（worker 不在）不记
        t = 1_800_000_000.0 // 3600 * 3600 + 1800
        state, snap = {"spans": [[t - 4000, t - 3000]]}, {"hosts": [{"id": "h"}]}
        C._long_term(state, snap, [{"t": t - 100, "host": "h", "util": [99, 99], "busy": [40, 60]}], {"interval": 120}, t - 100)
        C._long_term(state, snap, [{"t": t - 50, "host": "h", "util": [99, 99]}], {"interval": 120}, t - 50)
        C._long_term(state, snap, [{"t": t, "host": "h", "util": [0, 0], "busy": [100, None]}], {"interval": 120}, t)
        self.assertEqual(snap["spans"], [[t - 4000, t - 3000], [t - 100, t]])
        self.assertEqual(snap["util_hourly"], {"h": [[t - 1800, 75.0]]})


class RelaunchTest(unittest.TestCase):
    def test_relaunch_only_auto_hosts_once(self):
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("LABMON_STATE")
            os.environ["LABMON_STATE"] = td
            try:
                hosts = [{"id": "a", "label": "A", "auto": True, "status": {"t": 1}, "worker_up": False},
                         {"id": "b", "label": "B", "auto": False, "status": None},
                         {"id": "c", "label": "C", "auto": True, "stopped": "11-04", "status": None},
                         {"id": "d", "label": "D", "auto": True, "error": "ssh 超时"},
                         {"id": "e", "label": "E", "auto": True, "status": {"t": 1}, "worker_up": True}]
                hosts.append({"id": "f", "label": "F", "auto": True, "status_error": "status.json 解析不了"})   # 连得上、状态读不出：worker 多半还在，不拉
                calls = []
                msgs = O.relaunch({"hosts": hosts}, start=lambda h: calls.append(h) or (0, "队列 2 条\n"))
                O.relaunch({"hosts": hosts}, start=lambda h: calls.append(h) or (0, ""))     # 10 分钟内不再拉
                ev = records(host="a")
            finally:
                os.environ.pop("LABMON_STATE") if old is None else os.environ.__setitem__("LABMON_STATE", old)
            self.assertEqual(calls, ["a"])
            self.assertIn("已重新拉起", msgs[0])
            self.assertTrue(msgs[0].endswith("\n队列 2 条"))                                 # worker 的输出跟在消息后面，进带时间戳的服务日志
            self.assertEqual(ev[0]["detail"], "队列 2 条")


class DiskTest(unittest.TestCase):
    """共享盘：一级目录按名字归人；按前后两次扫描插值算近 24 小时增量（含新建、删掉的目录）；快满、暴涨通知，超提醒线只记事件。"""

    def test_owners_growth_and_alerts(self):
        from labmon import disk as DK
        G = 2 ** 30
        with tempfile.TemporaryDirectory() as td:
            env = {"LABMON_STATE": td, "LABMON_HOSTS": str(Path(td) / "none.toml")}
            old = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                t0 = 1_800_000_000
                (Path(td) / "overview" / "fleet").mkdir(parents=True)
                save_json(Path(td) / "overview" / "fleet" / "people.json", {"BOB": {}, "ALICE": {}})
                cfg = {"servers": [], "disk": {}}
                u = lambda b, t, **k: {"b": b * G, "n": 3, "m": t, "age": [b * G, 0, 0, 0, 0], "t": t, "s": 5, **k}
                ix = {"root": "/r", "host": "29", "t": t0, "listed": t0, "split": [],
                      "units": {"/r/bob/ckpt": u(100, t0), "/r/alice/a1": u(5, t0), "/r/adg/x": u(10, t0), "/r/.": u(0, t0)}}
                DK.absorb("123\n" + json.dumps(ix), t0)
                self.assertEqual(DK.seen(), 123)
                hosts = lambda free, t: [{"id": "h", "status": {"t": t, "info": {"disk": [{"path": "/r/alice", "free": free, "total": 1000}]}}}]
                v, msgs = DK.update(hosts(200, t0), t0, cfg)
                by = {o["name"]: o for o in v["owners"]}
                self.assertEqual([o["name"] for o in v["owners"]], ["BOB", "adg", "ALICE", "公共"])   # 大小写不同也归到名单里的人
                self.assertEqual((by["BOB"]["person"], by["adg"]["person"], by["BOB"]["d24"]), (True, False, 0))
                self.assertEqual((v["df"]["free"], msgs), (200 * G, []))
                cols = dict(v["colors"])
                self.assertEqual((sorted(cols), len(set(cols.values()))), (["ALICE", "BOB"], 2))   # 只给名单里的人；「公共」与对不上人的目录不占颜色
                t1 = t0 + 3600
                ix2 = {**ix, "t": t1, "listed": t1, "units": {"/r/bob/ckpt": u(150, t1, pb=100 * G, pt=t0), "/r/bob/new": u(20, t1),
                                                            "/r/alice/a1": u(5, t0), "/r/.": u(0, t0)}}
                DK.absorb("124\n" + json.dumps(ix2), t1)
                DK.absorb("124\n", t1)                                            # 没变：服务器不传，什么也不做
                v, msgs = DK.update(hosts(130, t1), t1, cfg)
                by = {o["name"]: o for o in v["owners"]}
                self.assertEqual(round(by["BOB"]["d24"] / G), 70)                  # 原有目录 +50，新建的 +20
                self.assertNotIn("adg", by)                                        # 整个删掉的目录不再单独列在按人表里（#179）
                self.assertEqual(round(v["d24"] / G), 60)                          # 删掉释放的 -10 GiB 仍算进总近 24 小时增量
                self.assertEqual(len(msgs), 1)
                self.assertIn("BOB 近 24 小时共享盘 +70 GiB", msgs[0])
                self.assertEqual(DK.update(hosts(130, t1 + 60), t1 + 60, cfg)[1], [])   # 已报过，不重报
                DK.set_limit("ALICE", 1)
                DK.set_owner("alice", "公共")
                v, msgs = DK.update(hosts(20, t1 + 120), t1 + 120, cfg)
                self.assertEqual([o["name"] for o in v["owners"]][:2], ["BOB", "公共"])
                self.assertEqual({k: v["colors"][k] for k in cols}, cols)          # 排名变了颜色不变
                self.assertEqual(len(msgs), 1)
                self.assertIn("只剩 20 GiB", msgs[0])
                DK.set_owner("alice", None)
                v, msgs = DK.update(hosts(20, t1 + 180), t1 + 180, cfg)
                self.assertEqual(msgs, [])                                         # 超过提醒线只记事件，不弹通知
                evs = [e["msg"] for e in load_rows(Path(td) / "overview" / "events.jsonl")]
                self.assertTrue(any("超过提醒线 1 GiB" in m for m in evs), evs)
                hrs = load_rows(Path(td) / "overview" / "fleet" / "disk" / "hours.jsonl")
                self.assertEqual(len(hrs), 2)                                      # 每小时一行
            finally:
                for k, v in old.items():
                    os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)

    def test_gone_only_owner_omitted(self):
        # #179：整个一级目录被删掉、只剩已删除单位时，不再进 v["owners"]，释放空间仍计入总 d24/d7；
        # 仍有现存单位的人删掉部分子目录时，照常留在 v["owners"] 并扣减自己的 d24/d7；未扫的现存目录照常保留 pending。
        from labmon import disk as DK
        G = 2 ** 30
        with tempfile.TemporaryDirectory() as td:
            env = {"LABMON_STATE": td, "LABMON_HOSTS": str(Path(td) / "none.toml")}
            old = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                t0 = 1_800_000_000
                (Path(td) / "overview" / "fleet").mkdir(parents=True)
                save_json(Path(td) / "overview" / "fleet" / "people.json", {"BOB": {}})
                cfg = {"servers": [], "disk": {}}
                u = lambda b, t: {"b": b * G, "n": 1, "m": t, "age": [b * G, 0, 0, 0, 0], "t": t, "s": 1}
                hosts = lambda t: [{"id": "h", "status": {"t": t, "info": {"disk": [{"path": "/r/bob", "free": 500, "total": 1000}]}}}]
                ix1 = {"root": "/r", "host": "h", "t": t0, "listed": t0, "split": [],
                       "units": {"/r/bob/keep": u(20, t0), "/r/bob/drop": u(8, t0), "/r/export_gone/a": u(12, t0)}}
                DK.absorb("1\n" + json.dumps(ix1), t0)
                DK.update(hosts(t0), t0, cfg)
                t1 = t0 + 3600
                ix2 = {"root": "/r", "host": "h", "t": t1, "listed": t1, "split": [],
                       "units": {"/r/bob/keep": u(20, t0), "/r/new_dir/wait": {"b": None, "t": None}}}
                DK.absorb("2\n" + json.dumps(ix2), t1)
                v, _ = DK.update(hosts(t1), t1, cfg)
                by = {o["name"]: o for o in v["owners"]}
                self.assertNotIn("export_gone", by)
                self.assertEqual(round(by["BOB"]["d24"] / G), -8)
                self.assertEqual(round(by["BOB"]["d7"] / G), -8)
                self.assertEqual((by["new_dir"]["t0"], by["new_dir"]["pending"]), (None, 1))
                self.assertEqual(round(v["d24"] / G), -20)                         # BOB -8 + export_gone -12
                self.assertEqual(round(v["d7"] / G), -20)
            finally:
                for k, v in old.items():
                    os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)

    def test_project_dirs_growth_and_job_writes(self):
        # #53：worker 把登记了清单的项目根拆开扫（项目根下的一级目录各一个单位）。新拆开的子单位不算新建（不误报暴涨），
        # 已拆开的目录下新建的照常从 0 算（以前 split 里任何目录下新建的都漏掉）；按项目合计增量，涨得快标 burst（disk: 告警）；
        # 任务写入量：开跑前最后一次扫描到跑完那次重扫之差，超过 bigwrite_gb 标 big；告警由 collect._storage 发，disk: 两边都给、bigwrite: 只给会话
        from labmon import disk as DK
        G = 2 ** 30
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"LABMON_STATE": td, "LABMON_HOSTS": str(Path(td) / "hosts.toml")}):
            (Path(td) / "hosts.toml").write_text('[[hosts]]\nid = "h"\ndata_root = "/r/alice"\n')
            (Path(td) / "overview" / "fleet").mkdir(parents=True)
            save_json(Path(td) / "overview" / "fleet" / "people.json", {"ALICE": {}})
            cfg = {"servers": [], "disk": {"burst_gb": 20}}
            t0 = 1_800_000_000
            u = lambda b, t, **k: {"b": b * G, "n": 3, "m": t, "age": [b * G, 0, 0, 0, 0], "t": t, "s": 5, **k}
            pa, pb = "/r/alice/projects/pa", "/r/alice/projects/pb"
            ix = {"root": "/r", "host": "h", "t": t0, "listed": t0, "split": [], "units": {"/r/alice/projects": u(100, t0), "/r/alice/.": u(0, t0)}}
            DK.absorb("1\n" + json.dumps(ix), t0)
            t1 = t0 + 600                                                  # 拆开：项目根与它的父目录进 split，原来那个单位消失
            ix = {**ix, "t": t1, "listed": t1, "split": ["/r/alice/projects", pa],
                  "units": {f"{pa}/cache": u(60, t1), f"{pa}/out": u(40, t1), f"{pa}/.": u(0, t1), pb: u(0, t1), "/r/alice/.": u(0, t0)}}
            DK.absorb("2\n" + json.dumps(ix), t1)
            hosts = lambda t, run=(): [{"id": "h", "status": {"t": t, "info": {"disk": [{"path": "/r/alice", "free": 130, "total": 1000}]},
                                                             "storage": {"projects": {pa: "pa", pb: "pb"}}, "running": list(run)}}]
            v, _ = DK.update(hosts(t1), t1, cfg)
            self.assertEqual(round(v["owners"][0]["d24"] / G), 0)          # 拆出来的不算新建
            p = v["ours"]["projects"]["pa"]
            self.assertEqual((round(p["b"] / G), p["d24"], p["burst"], p["roots"]), (100, 0, False, [pa]))
            s0, e0 = t1 + 100, t1 + 3000                                    # 一个任务在两次扫描之间跑完
            append = __import__("labmon.state", fromlist=["append_row"]).append_row
            from labmon.state import usage_file
            append(usage_file("h"), {"t": e0, "s": s0, "n": "j1", "p": "pa", "root": pa + "/run", "queue": "q.txt", "st": "done"}, 30 * 86400, e0)
            append(usage_file("h"), {"t": e0 - 60, "s": s0 + 60, "n": "j2", "p": "pa", "root": pa + "/run", "queue": "q.txt", "st": "failed"}, 30 * 86400, e0)
            t2 = t1 + 3600                                                  # 跑完那次重扫：cache +7、新建的 new +20
            ix = {**ix, "t": t2, "listed": t2, "units": {f"{pa}/cache": u(67, t2, pb=60 * G, pt=t1), f"{pa}/out": u(40, t2), f"{pa}/.": u(0, t2),
                                                          f"{pa}/new": u(20, t2), pb: u(0, t2), "/r/alice/.": u(0, t0)}}
            DK.absorb("3\n" + json.dumps(ix), t2)
            v, _ = DK.update(hosts(t2, [{"project": "pb", "name": "x", "t0": s0}]), t2, cfg)
            self.assertEqual(round(v["owners"][0]["d24"] / G), 27)         # 已拆开的目录下新建的从 0 算
            p = v["ours"]["projects"]["pa"]
            self.assertEqual((round(p["d24"] / G), p["burst"], p["pending"]), (27, True, 0))
            j1 = next(j for j in p["jobs"] if j["name"] == "j1")
            self.assertEqual((round(j1["b"] / G), [(n, round(b / G)) for n, b in j1["top"]], j1["from"], j1["to"], j1["with"], j1["wait"], j1["big"]),
                             (27, [("new", 20), ("cache", 7)], t1, t2, 1, False, True))   # 同期 j2 也算在内；别的项目在跑的不算
            snap_g = {"disk": v, "hosts": hosts(t2)}
            st = DK.project_storage("pa", snap_g)
            self.assertEqual(sorted(d["name"] for d in st["dirs"]), [".", "cache", "new", "out"])
            self.assertEqual((st["roots"], st["free"], st["bigwrite_gb"]), ([pa], 130 * G, 5))
            self.assertEqual(DK.project_storage("pb", snap_g)["jobs"], [])
            snap = {"alerts": []}
            C._storage({"id": "pa"}, snap, snap_g, e0 + 600)
            al = {a["key"]: a for a in snap["alerts"]}
            self.assertEqual(al["disk:pa"]["to"], "both")
            self.assertIn("共享盘上本项目近 24 小时 +27 GiB（new +20 GiB）", al["disk:pa"]["msg"])
            self.assertIn("$LABMON_CACHE", al["disk:pa"]["msg"])
            self.assertNotIn("$LABMON_CACHE", al["disk:pa"]["page"])
            bw = [a for a in snap["alerts"] if a["key"].startswith("bigwrite:")]
            self.assertEqual([(a["key"], a["to"]) for a in bw], [(f"bigwrite:j2:{e0 - 60}", "agent")])   # 同一对扫描量出来的两个任务合成一条
            self.assertIn("j2、j1 跑的这段时间本项目在共享盘上净增 27 GiB，大头 new +20 GiB：", bw[0]["msg"])
            self.assertEqual(snap["storage"]["d24"], p["d24"])
            snap = {"alerts": []}
            C._storage({"id": "pa"}, snap, snap_g, e0 + 86400)               # 结束一天后不再报 bigwrite
            self.assertFalse(any(a["key"].startswith("bigwrite:") for a in snap["alerts"]))
            v, _ = DK.update(hosts(t2 + 60), t2 + 60, {"servers": [], "disk": {}})
            self.assertFalse(v["ours"]["projects"]["pa"]["burst"])           # 门槛与按人的 burst 同一条：30 GiB 以下不算

    def test_project_writes_split_history_slots_and_scan_sets(self):
        # #53 审查意见（PR #56）：
        # 1. 拆开前整个项目根扫过的旧单位不再扫，不算等重扫（否则拆开后结束的任务一直 wait、bigwrite 漏报）；
        # 2. worker 先把父目录并进 split、去掉父单位，子单位下一份索引才出现时，子单位照样不算新建（不误报暴涨）；
        # 3. 单元申请的用量行（没有 root）不算任务写入；
        # 4. 合不合成一条按实际用到的那组扫描，不按最外侧两个时刻：先后跑的两个任务各报各的
        from labmon import disk as DK
        from labmon.state import append_row, usage_file
        G = 2 ** 30
        u = lambda b, t, **k: {"b": b * G, "n": 3, "m": t, "age": [b * G, 0, 0, 0, 0], "t": t, "s": 5, **k}
        pa = "/r/alice/projects/pa"
        hosts = lambda t: [{"id": "h", "status": {"t": t, "info": {"disk": [{"path": "/r/alice", "free": 500 * G, "total": 1000 * G}]},
                                                 "storage": {"projects": {pa: "pa"}}, "running": []}}]
        cfg = {"servers": [], "disk": {"burst_gb": 20}}
        use = lambda n, s, e, root=pa + "/run": append_row(usage_file("h"), {"t": e, "s": s, "n": n, "p": "pa", "root": root, "queue": "q.txt" if root else None, "st": "done"}, 30 * 86400, e)

        import shutil

        def board():
            td = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, td, True)
            ctx = mock.patch.dict(os.environ, {"LABMON_STATE": td, "LABMON_HOSTS": str(Path(td) / "hosts.toml")})
            ctx.start()
            self.addCleanup(ctx.stop)
            (Path(td) / "hosts.toml").write_text('[[hosts]]\nid = "h"\ndata_root = "/r/alice"\n')
            (Path(td) / "overview" / "fleet").mkdir(parents=True, exist_ok=True)

        t0 = 1_800_000_000
        board()                                                          # 1：项目根原来整个是一个单位、扫过几次，后来拆开
        ix = {"root": "/r", "host": "h", "t": t0, "listed": t0, "split": ["/r/alice/projects"], "units": {pa: u(100, t0), "/r/alice/.": u(0, t0)}}
        DK.absorb("1\n" + json.dumps(ix), t0)
        t1 = t0 + 600
        ix = {**ix, "t": t1, "listed": t1, "split": ["/r/alice/projects", pa], "units": {f"{pa}/out": u(100, t1), "/r/alice/.": u(0, t0)}}
        DK.absorb("2\n" + json.dumps(ix), t1)
        use("j", t1 + 60, t1 + 120, root=pa + "/run")
        use("slot1", t1 + 60, t1 + 120, root=None)                       # 3：单元申请
        t2 = t1 + 600
        ix = {**ix, "t": t2, "listed": t2, "units": {f"{pa}/out": u(110, t2), "/r/alice/.": u(0, t0)}}
        DK.absorb("3\n" + json.dumps(ix), t2)
        v, _ = DK.update(hosts(t2), t2, cfg)
        jobs = {j["name"]: j for j in v["ours"]["projects"]["pa"]["jobs"]}
        self.assertEqual(sorted(jobs), ["j"])
        self.assertEqual((round(jobs["j"]["b"] / G), jobs["j"]["pending"], jobs["j"]["wait"], jobs["j"]["big"]), (10, 0, False, True))

        board()                                                          # 2：拆开分两份索引到看板
        ix = {"root": "/r", "host": "h", "t": t0, "listed": t0, "split": ["/r/alice/projects"], "units": {pa: u(100, t0), "/r/alice/.": u(0, t0)}}
        DK.absorb("1\n" + json.dumps(ix), t0)
        ix = {**ix, "t": t0 + 60, "split": ["/r/alice/projects", pa], "units": {"/r/alice/.": u(0, t0)}}   # Disk.done：父单位去掉、子单位还没列
        DK.absorb("2\n" + json.dumps(ix), t0 + 60)
        ix = {**ix, "t": t1, "listed": t1, "units": {f"{pa}/out": u(100, t1), "/r/alice/.": u(0, t0)}}
        DK.absorb("3\n" + json.dumps(ix), t1)
        v, _ = DK.update(hosts(t1), t1, cfg)
        self.assertEqual(v["ours"]["projects"]["pa"]["d24"], 0)
        ix = {**ix, "t": t2, "listed": t2, "units": {f"{pa}/out": u(100, t2), f"{pa}/new": u(3, t2), "/r/alice/.": u(0, t0)}}
        DK.absorb("4\n" + json.dumps(ix), t2)
        v, _ = DK.update(hosts(t2), t2, cfg)
        self.assertEqual(round(v["ours"]["projects"]["pa"]["d24"] / G), 3)   # 已列过的目录下新建的照常从 0 算

        board()                                                          # 5：开跑前是 0、还在索引里的目录没重扫：等，不当作删掉了
        C0, O = f"{pa}/cache", f"{pa}/out"
        ix = {"root": "/r", "host": "h", "t": t0, "listed": t0, "split": ["/r/alice/projects", pa], "units": {C0: u(0, t0), O: u(100, t0)}}
        DK.absorb("1\n" + json.dumps(ix), t0)
        use("z", t0 + 60, t0 + 120)
        ix = {**ix, "t": t1, "listed": t1, "units": {C0: u(0, t0), O: u(110, t1)}}
        DK.absorb("2\n" + json.dumps(ix), t1)
        v, _ = DK.update(hosts(t1), t1, cfg)
        z = v["ours"]["projects"]["pa"]["jobs"][0]
        self.assertEqual((z["pending"], z["wait"], z["big"]), (1, True, False))

        board()                                                          # 4：A 扫得勤、B 扫得慢；两个任务先后跑，最外侧时刻相同
        T = [t0, t0 + 1000, t0 + 2000, t0 + 3000]
        A, B = f"{pa}/a", f"{pa}/b"
        ix = {"root": "/r", "host": "h", "t": T[0], "listed": T[0], "split": ["/r/alice/projects", pa], "units": {A: u(100, T[0]), B: u(50, T[0])}}
        DK.absorb("1\n" + json.dumps(ix), T[0])
        for i, (a, ub) in enumerate([(u(110, T[1]), u(50, T[0])), (u(130, T[2]), u(50, T[0])), (u(130, T[3]), u(55, T[3]))], 1):
            ix = {**ix, "t": T[i], "listed": T[i], "units": {A: a, B: ub}}
            DK.absorb(f"{i + 1}\n" + json.dumps(ix), T[i])
        use("k1", T[0] + 100, T[1] - 100)
        use("k2", T[1] + 100, T[2] - 100)
        v, _ = DK.update(hosts(T[3]), T[3], cfg)
        js = {j["name"]: j for j in v["ours"]["projects"]["pa"]["jobs"]}
        self.assertEqual((round(js["k1"]["b"] / G), round(js["k2"]["b"] / G)), (15, 25))
        snap = {"alerts": []}
        C._storage({"id": "pa"}, snap, {"disk": v, "hosts": hosts(T[3])}, T[3])
        bw = sorted(a["key"] for a in snap["alerts"] if a["key"].startswith("bigwrite:"))
        self.assertEqual(bw, [f"bigwrite:k1:{T[1] - 100}", f"bigwrite:k2:{T[2] - 100}"])

    def test_rows_cache_sees_second_write_within_same_mtime(self):
        # #31：scans.jsonl 同一个修改时刻里续写一行也要读到（以前只比 st_mtime，漏读到下次写）；换了状态目录不拿另一个目录的缓存
        from labmon import disk as DK
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"LABMON_STATE": td}):
            f = DK._f("scans.jsonl")
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps({"u": "a"}) + "\n")
            self.assertEqual([r["u"] for r in DK._rows("scans.jsonl")], ["a"])
            ns = f.stat().st_mtime_ns
            with open(f, "a") as fh:
                fh.write(json.dumps({"u": "b"}) + "\n")
            os.utime(f, ns=(ns, ns))
            self.assertEqual([r["u"] for r in DK._rows("scans.jsonl")], ["a", "b"])
            with tempfile.TemporaryDirectory() as td2, mock.patch.dict(os.environ, {"LABMON_STATE": td2}):
                self.assertEqual(DK._rows("scans.jsonl"), [])


class LinkStepTest(unittest.TestCase):
    """掉线判定的边界：连不上要连续至少 2 次、且从第一次起持续 DOWN_S 秒才算；判过一次不再判；连上了才算恢复、清计数。"""

    def test_boundaries(self):
        hs, t = {}, 1_800_000_000
        self.assertIsNone(link_step(hs, False, t))                          # 第一次：记起点
        self.assertEqual((hs["fails"], hs["since"]), (1, t))
        self.assertIsNone(link_step(hs, False, t + DOWN_S - 1))           # 差 1 秒：还不算
        self.assertEqual(link_step(hs, False, t + DOWN_S), "down")        # 刚够：掉线（之后由调用方写 hs["down"]）
        hs["down"] = "连不上"
        self.assertIsNone(link_step(hs, False, t + DOWN_S + 100))         # 已判过：不再报
        self.assertEqual(hs["fails"], 4)
        self.assertEqual(link_step(hs, True, t + 3600), "up")               # 连上：恢复，计数清零，since 留着给调用方算离线多久
        self.assertEqual((hs["fails"], hs["down"], hs["since"]), (0, None, t))
        self.assertIsNone(link_step(hs, True, t + 3700))                    # 一直连着：没事
        self.assertIsNone(link_step(hs, False, t + 4000))                   # 抖一下
        self.assertIsNone(link_step(hs, True, t + 4060))                    # 没判掉线就恢复：不报恢复
        self.assertEqual((hs["fails"], hs["since"]), (0, t + 4000))
        hs = {"fails": 0, "down": None}
        link_step(hs, False, t)
        self.assertEqual(link_step(hs, False, t + 10 * DOWN_S), "down")   # 第 2 次离第 1 次够久：第 2 次就算
        self.assertIsNone(link_step({"fails": 0, "down": None}, False, t + 10 * DOWN_S))   # 只 1 次：再久也不算


class HealthTest(unittest.TestCase):
    """连不上持续约 3 分钟才算掉线，按租期与平台能否打开给原因；恢复、换容器、租期将到都记事件。"""

    def run_health(self, td, err, t, started=None, platform=True):
        os.environ["LABMON_STATE"] = td
        e = {"id": "a", "label": "A", "error": err,
             "status": None if err else {"info": {"started": started}}}
        cfg = {"a": {"lease_days": 7, "platform": "http://p/"}}
        msgs = O.health([e], cfg, t, probe=lambda url: platform)
        return e, msgs

    def test_down_reason_and_recover(self):
        old = os.environ.get("LABMON_STATE")
        try:
            with tempfile.TemporaryDirectory() as td:
                t0 = 1_800_000_000
                save_json(Path(td) / "overview" / "health.json", {"a": {"fails": 0, "down": None, "warned": [], "disk_low": False, "disk_soon": False}})
                self.run_health(td, None, t0, started=t0 - 86400)
                self.assertEqual(set(json.loads((Path(td) / "overview" / "health.json").read_text())["a"]), {"fails", "down", "warned", "started", "miss", "lease_end"})   # 旧告警的键不再写回
                e, m = self.run_health(td, "ssh 超时", t0 + 60)
                self.assertEqual((e["health"]["state"], m), ("suspect", []))          # 抖一下不报
                e, m = self.run_health(td, "ssh 超时", t0 + 120)
                self.assertEqual(m, [])
                e, m = self.run_health(td, "ssh 超时", t0 + 240)
                self.assertEqual(e["health"]["state"], "down")
                self.assertIn("作业可能被平台停掉", m[0])
                self.assertEqual(self.run_health(td, "ssh 超时", t0 + 300)[1], [])     # 掉线只报一次
                e, m = self.run_health(td, None, t0 + 3600, started=t0 + 3000)
                self.assertIn("恢复连接", m[0])
                evs = [json.loads(l) for l in (Path(td) / "overview" / "events.jsonl").read_text().splitlines()]
                self.assertEqual([x["kind"] for x in evs], ["host_down", "host_up", "container_new"])
                self.assertEqual(evs[-1]["started"], t0 + 3000)                        # 新容器的启动时刻随事件记下，项目页的里程碑取它
                self.assertEqual(e["lease_end"], t0 + 3000 + 7 * 86400)                # 新容器起算新租期
        finally:
            os.environ.pop("LABMON_STATE") if old is None else os.environ.__setitem__("LABMON_STATE", old)

    def test_bad_status_not_down(self):
        # 连得上、status.json 读不出（status_error）：不进掉线判定，连不上的计数也清零
        old = os.environ.get("LABMON_STATE")
        try:
            with tempfile.TemporaryDirectory() as td:
                t0 = 1_800_000_000
                self.run_health(td, "ssh 超时", t0)
                os.environ["LABMON_STATE"] = td
                e = {"id": "a", "label": "A", "error": None, "status_error": "status.json 解析不了", "status": None}
                for k in range(3):
                    msgs = O.health([e], {"a": {}}, t0 + 100 + k * 100, probe=lambda url: True)
                self.assertEqual((e["health"]["state"], msgs), ("ok", []))
        finally:
            os.environ.pop("LABMON_STATE") if old is None else os.environ.__setitem__("LABMON_STATE", old)

    def test_lease_warnings_and_platform_down(self):
        old = os.environ.get("LABMON_STATE")
        try:
            with tempfile.TemporaryDirectory() as td:
                s0 = 1_800_000_000
                end = s0 + 7 * 86400
                self.assertEqual(self.run_health(td, None, end - 30 * 3600, started=s0)[1], [])
                m = self.run_health(td, None, end - 20 * 3600, started=s0)[1]
                self.assertEqual(len(m), 1)
                self.assertIn("租期约剩 20 小时", m[0])
                self.assertEqual(self.run_health(td, None, end - 19 * 3600, started=s0)[1], [])   # 同一档不重复
                self.assertEqual(len(self.run_health(td, None, end - 5 * 3600, started=s0)[1]), 1)
                for k in range(3):                                          # 平台也打不开
                    _, m = self.run_health(td, "ssh 超时", end - 4 * 3600 + k * 100, platform=False)
                self.assertIn("平台网页也打不开", m[0])
            with tempfile.TemporaryDirectory() as td:
                self.run_health(td, None, s0, started=s0)
                for k in range(3):                                          # 到期后掉线：归为到期
                    _, m = self.run_health(td, "ssh 超时", end + k * 100)
                self.assertIn("租期到了", m[0])
        finally:
            os.environ.pop("LABMON_STATE") if old is None else os.environ.__setitem__("LABMON_STATE", old)


class WaitTest(unittest.TestCase):
    def test_alerts_and_stale(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "m.toml").write_text(f'out_dir = "{d}"\n')
            cfg = load(d / "m.toml")
            sf = d / "wait_state.json"
            wait = lambda: wait_check(cfg, d, sf, d / "overview.json")     # 不读真的全局快照（借到卡等全局消息）
            self.assertIn("collector_stale", wait())                        # 还没有快照
            snap = {"generated": time.time(), "alerts": [{"key": "gpu_idle:x", "msg": "GPU 闲"}]}
            save_json(d / "snapshot.json", snap)
            self.assertEqual(wait(), {"gpu_idle:x": "GPU 闲"})
            self.assertEqual(wait(), {})                                   # 同一条不重复报
            snap["generated"] -= 3600
            save_json(d / "snapshot.json", snap)
            self.assertEqual(list(wait()), [])                             # 报过的 collector_stale 消失后 6 小时内再出现：不再叫醒（#88）
            st = json.loads(sf.read_text())
            st["reported"].remove("collector_stale")                      # 又消失过一次、最后一次在场是 6 小时前
            sf.write_text(json.dumps(dict(st, last=dict(st["last"], collector_stale=st["last"]["collector_stale"] - W.REWAKE_S))))
            self.assertEqual(list(wait()), ["collector_stale"])            # 过了 6 小时再出现照报

    def test_user_only_and_rewake(self):
        """#88：只给用户的告警（ssh_fail:）不叫醒会话；报过的消失后 REWAKE_S 内再出现不叫醒、过了再报；
        failed: 每次出现都报（删了 .failed 重跑又失败）。旧代码：ssh_fail 照报，消失再出现都再叫醒。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "m.toml").write_text(f'out_dir = "{d}"\n')
            cfg = load(d / "m.toml")
            sf = d / "wait_state.json"

            def wait(*alerts):
                save_json(d / "snapshot.json", {"generated": time.time(), "alerts": [{"key": k, "to": to, "msg": k} for k, to in alerts]})
                return sorted(wait_check(cfg, d, sf, d / "overview.json"))
            self.assertEqual(wait(("ssh_fail:a", "user"), ("disk:x", "both"), ("no_work", "agent"), ("failed:j", "agent")),
                             ["disk:x", "failed:j", "no_work"])
            self.assertEqual(wait(("ssh_fail:a", "user"), ("no_work", "agent")), [])               # disk、failed 消失；no_work 还在
            self.assertEqual(wait(("disk:x", "both"), ("no_work", "agent"), ("failed:j", "agent")), ["failed:j"])   # disk 6 小时内回来：不报
            st = json.loads(sf.read_text())
            self.assertNotIn("ssh_fail:a", st["reported"])
            old = {k: v - W.REWAKE_S - 1 for k, v in st["last"].items()}
            sf.write_text(json.dumps(dict(st, reported=[], last=old)))
            self.assertEqual(wait(("disk:x", "both"), ("no_work", "agent")), ["disk:x", "no_work"])    # 过了 REWAKE_S：照报

    def test_stale_limit(self):
        """collector_stale 的门槛 max(3×interval, stale_min) + 1 分钟（config.stale_limit；项目页标红、配置抽屉的说法同一个，#141）。"""
        self.assertEqual((stale_limit(120, 5), stale_limit(120, 10), stale_limit(60, 1)), (420, 660, 240))
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "m.toml").write_text(f'out_dir = "{d}"\ninterval = 120\n[alerts]\nstale_min = 5\n')
            cfg = load(d / "m.toml")
            sf = d / "wait_state.json"
            for ago, want in ((6 * 60, []), (8 * 60, ["collector_stale"])):   # 过了 stale_min、没到 3 次采集 + 1 分钟的不报
                save_json(d / "snapshot.json", {"generated": time.time() - ago, "alerts": []})
                self.assertEqual(list(wait_check(cfg, d, sf, d / "overview.json")), want, ago)


if __name__ == "__main__":
    unittest.main()


class StateFilesTest(unittest.TestCase):
    """本机状态目录里的 JSONL：坏行、原子写、给 worker 的文件只在真写成时记已发。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("LABMON_STATE")
        os.environ["LABMON_STATE"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("LABMON_STATE") if self.old is None else os.environ.__setitem__("LABMON_STATE", self.old)
        self.tmp.cleanup()

    def test_keep_log_skips_broken_line(self):
        # 主机日志超过 3000 行整理时遇到写了一半的行（服务被杀在写的半途）：丢掉它，不抛异常让整轮采集失败
        sd = state_dir()
        f = sd / "hostlog" / "h.jsonl"
        f.parent.mkdir(parents=True)
        t = time.time()
        f.write_text("".join(json.dumps({"t": t - i, "kind": "wait", "msg": "x"}) + "\n" for i in range(3001)) + '{"t": 1, "kind": "wa')
        O._keep_log(sd, "h", [{"t": t, "kind": "override", "msg": "new"}, {"t": t - 40 * 86400, "kind": "override", "msg": "old"}], t)
        rows = load_rows(f)
        self.assertEqual((len(rows), rows[-1]["msg"]), (3002, "new"))
        self.assertEqual(len(f.read_text().splitlines()), 3002)

    def test_put_only_counted_when_written(self):
        # worker 目录还没建（没 start 过）时 sched.json 写不进去：不记成已发，目录出现后那一轮补写；之后内容不变不再写
        O._SENT.clear()
        wh = Path(self.tmp.name) / "wh"
        h = {"id": "h1", "worker_home": str(wh), "ssh": None, "port": 22, "put": {"sched.json": {"weight": {"a1": 2}}, "eta.json": {"t": 1, "jobs": {}}}}
        raw, err = O.fetch(h)
        self.assertEqual((err, dict(O._SENT)), (None, {}))
        (wh / "hosts" / "h1").mkdir(parents=True)
        raw, err = O.fetch(h)
        self.assertEqual(json.loads((wh / "hosts" / "h1" / "sched.json").read_text()), {"weight": {"a1": 2}})
        self.assertEqual(O._SENT, {("h1", "sched.json"): {"weight": {"a1": 2}}, ("h1", "eta.json"): {"jobs": {}}})
        self.assertEqual(len(raw.split(O.SEP + "\n")), 9)                        # 回显的那一段已经摘掉，collect 照旧按段解析（第 8 段是登记，只有 h["regs"] 的那台有内容；第 9 段是服务租约，#163）
        (wh / "hosts" / "h1" / "sched.json").unlink()
        O.fetch(h)
        self.assertFalse((wh / "hosts" / "h1" / "sched.json").exists())
        h["put"]["eta.json"] = {"t": 2, "jobs": {}}                            # 只有写的时刻变了：不算变
        O.fetch(h)
        self.assertEqual(json.loads((wh / "hosts" / "h1" / "eta.json").read_text()), {"t": 1, "jobs": {}})
        h["put"]["eta.json"] = {"t": 3, "jobs": {"a1|x": 60}}                 # 预计结束变了：写，并记新内容
        O.fetch(h)
        self.assertEqual(json.loads((wh / "hosts" / "h1" / "eta.json").read_text()), {"t": 3, "jobs": {"a1|x": 60}})
        self.assertEqual(O._SENT[("h1", "eta.json")], {"jobs": {"a1|x": 60}})
        self.assertIsNone(O.fetch({**h, "id": "h2"})[1])                      # 另一台（目录没有）：不记已发，也不影响 h1 的
        self.assertNotIn(("h2", "eta.json"), O._SENT)
        self.assertEqual(O._SENT[("h1", "eta.json")], {"jobs": {"a1|x": 60}})

    def test_to_worker_eta_prog(self):
        # #149：eta.json 的 prog 只放真正按日志进度外推的任务（近期速度、进度、进度（停了一阵）），估时回声与兜底不进 prog；jobs 照旧全给
        projects = [{"id": "p", "running": [
            {"id": "j_recent", "eta_ts": 1200, "eta_source": "近期速度"},
            {"id": "j_prog", "eta_ts": 1260, "eta_source": "进度"},
            {"id": "j_stall", "eta_ts": 1320, "eta_source": "进度（停了一阵）"},
            {"id": "j_est", "eta_ts": 1380, "eta_source": "清单估时"},
            {"id": "j_first", "eta_ts": 1440, "eta_source": "估时（进度刚开始）"},
            {"id": "j_multi", "eta_ts": 1500, "eta_source": "估时（多段任务，进度只算当前这段）"},
            {"id": "j_over", "eta_ts": 1560, "eta_source": "已超估时"},
            {"id": "j_none", "eta_ts": None, "eta_source": "无估时"},
        ]}]
        _, put_fn = O._to_worker(projects, 1000.0, {})
        eta = put_fn({"id": "h1"})["eta.json"]
        self.assertEqual(set(eta["jobs"]), {"p|j_recent", "p|j_prog", "p|j_stall", "p|j_est", "p|j_first", "p|j_multi", "p|j_over"})
        self.assertEqual(eta["prog"], ["p|j_prog", "p|j_recent", "p|j_stall"])

    def test_occupancy_since_skip_and_lanes(self):
        # 只取 since 之后结束的；skip 的项目不给（项目页只看同机别的项目占卡，默认不含纯 CPU）；车道名按项目看板登记的 (主机, 目录, 清单) 对上；
        # 非占位纯 CPU 任务（已跑、在跑、推演排队）在总览保留 g=[]，占位任务跳过（#207）
        f = state_dir() / "usage" / "h.jsonl"
        f.parent.mkdir(parents=True)
        f.write_text("".join(json.dumps(r) + "\n" for r in [
            {"t": 200, "s": 100, "n": "early", "p": "a", "g": ["G0"], "gi": [0], "st": "done", "root": "/r", "queue": "q.txt"},
            {"t": 400, "s": 300, "n": "mine", "p": "a", "g": ["G0"], "gi": [0], "st": "failed", "root": "/r", "queue": "q.txt"},
            {"t": 500, "s": 450, "n": "theirs", "p": "b", "g": ["G0"], "gi": [0], "st": "stop", "root": "/o", "queue": "q.txt"},
            {"t": 600, "s": 550, "n": "cpu", "p": "b", "g": [], "gi": [], "st": "done", "root": "/o", "queue": "q.txt"},
            {"t": 610, "s": 560, "n": "hold_done", "p": "b", "g": [], "gi": [], "st": "done"}]))
        hosts = [{"id": "h", "status": {"info": {"gpus": [{"i": 0, "uuid": "G0"}]},
                                        "plan": {"lanes": [{"root": "/r", "queue": "q.txt", "project": "a", "jobs": [
                                                            {"name": "a2", "t0": 700, "t1": 800, "gpus": [0]},
                                                            {"name": "cpu_run", "t0": 680, "t1": 740, "gpus": [], "run": True},
                                                            {"name": "cpu_plan", "t0": 740, "t1": 820, "gpus": [], "after": [["a", "cpu_run"]]}]},
                                                           {"root": "/o", "queue": "q.txt", "project": "b", "jobs": [
                                                            {"name": "b2", "t0": 700, "t1": 900, "gpus": [0], "after": [["a", "a2"]]},
                                                            {"name": "hold", "t0": 700, "t1": None, "gpus": []},
                                                            {"name": "hold_wait", "t0": 700, "t1": 760, "gpus": []}]}]}}}]
        projects = [{"id": "a", "lanes": [{"host": "h", "root": "/r", "queue": "q.txt", "label": "车道 1 训练"}]},
                    {"id": "b", "lanes": [{"host": "h", "root": "/o", "queue": "q.txt", "label": "车道 2"}]}]
        occ = O.occupancy(hosts, projects, 250)
        self.assertEqual([(r["n"], r["lane"], r["g"], r["st"], r.get("after")) for r in occ["h"]],
                         [("mine", "车道 1 训练", [0], "failed", None), ("theirs", "车道 2", [0], "stop", None),
                          ("cpu", "车道 2", [], "done", None), ("a2", "车道 1 训练", [0], "plan", None),
                          ("cpu_run", "车道 1 训练", [], "run", None), ("cpu_plan", "车道 1 训练", [], "plan", [["a", "cpu_run"]]),
                          ("b2", "车道 2", [0], "plan", [["a", "a2"]])])
        self.assertEqual([r["n"] for r in O.occupancy(hosts, projects, 250, skip="a")["h"]], ["theirs", "b2"])
        self.assertEqual([r["n"] for r in O.occupancy(hosts, projects, 250, skip="a", cpu=True)["h"]], ["theirs", "cpu", "b2"])
        self.assertEqual(O.occupancy(hosts, [], 1000)["h"], [{"p": "a", "n": "a2", "lane": None, "t0": 700, "t1": 800, "g": [0], "st": "plan"},
                                                             {"p": "a", "n": "cpu_run", "lane": None, "t0": 680, "t1": 740, "g": [], "st": "run"},
                                                             {"p": "a", "n": "cpu_plan", "lane": None, "t0": 740, "t1": 820, "g": [], "st": "plan", "after": [["a", "cpu_run"]]},
                                                             {"p": "b", "n": "b2", "lane": None, "t0": 700, "t1": 900, "g": [0], "st": "plan", "after": [["a", "a2"]]}])

    def test_occupancy_after_new_container(self):
        # 换容器后 GPU 的 uuid 全变：历史占用条按记录时的卡序号 gi 画，uuid 还对得上的仍按此刻的序号
        f = state_dir() / "usage" / "h.jsonl"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps({"t": 200, "s": 100, "n": "old", "p": "a", "g": ["GPU-old"], "gi": [1], "st": "done"}) + "\n"
                     + json.dumps({"t": 300, "s": 250, "n": "cur", "p": "a", "g": ["GPU-new"], "gi": [0], "st": "done"}) + "\n"
                     + json.dumps({"t": 400, "s": 350, "n": "legacy", "p": "a", "g": ["GPU-gone"], "st": "done"}) + "\n")   # 11-07 前的记录没有 gi：画不了
        occ = O.occupancy([{"id": "h", "status": {"info": {"gpus": [{"i": 3, "uuid": "GPU-new"}]}}}], [], 0)
        self.assertEqual([(r["n"], r["g"]) for r in occ["h"]], [("old", [1]), ("cur", [3])])

    def test_proj_events_includes_other_cpu_and_excludes_other_member_auto(self):
        # #244：项目页 events.json 的 occ 包含同机其他项目纯 CPU 任务（与 GPU 任务对称），并排除他人 member_auto 主机的实时占用
        now = time.time()
        f = state_dir() / "usage" / "h.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"t": now - 100, "s": now - 300, "n": "cpu_done", "p": "b", "g": [], "st": "done"}) + "\n")
        save_json(state_dir() / "snapshot.json", {
            "hosts": [
                {"id": "h", "status": {
                    "info": {"gpus": [{"i": 0, "uuid": "G0"}]},
                    "plan": {"lanes": [
                        {"root": "/r", "queue": "q.txt", "project": "a", "jobs": [{"name": "a_gpu", "t0": now - 60, "t1": now + 60, "gpus": [0], "run": True}]},
                        {"root": "/o", "queue": "q.txt", "project": "b", "jobs": [
                            {"name": "b_cpu_run", "t0": now - 50, "t1": now + 50, "gpus": [], "run": True},
                            {"name": "b_gpu_plan", "t0": now + 60, "t1": now + 180, "gpus": [0]},
                            {"name": "hold_b", "t0": now, "t1": now + 30, "gpus": []},
                        ]},
                    ]},
                }},
                {"id": "srv-alice", "member_auto": True, "member_account": "alice", "status": {
                    "info": {"gpus": [{"i": 0, "uuid": "GA"}]},
                    "plan": {"lanes": [{"root": "/a", "queue": "q.txt", "project": "p-alice", "jobs": [
                        {"name": "alice_priv", "t0": now - 20, "t1": now + 80, "gpus": [0], "run": True}
                    ]}]},
                }},
            ],
            "projects": [{"id": "b", "lanes": [{"host": "h", "root": "/o", "queue": "q.txt", "label": "车道 1"}]}],
            "colors": {"a": "#7F77DD", "b": "#D85A30"},
        })

        class FakeProj:
            id = "a"

            def snapshot(self):
                return {"hosts": [{"id": "h"}, {"id": "srv-alice", "returned": now - 3600}]}

        ev = S.proj_events(FakeProj())
        self.assertEqual([(r["n"], r["g"], r["st"]) for r in ev["occ"]["h"]],
                         [("cpu_done", [], "done"), ("b_cpu_run", [], "run"), ("b_gpu_plan", [0], "plan")])
        self.assertNotIn("srv-alice", ev["occ"])

    def test_write_page_escapes_all_lt(self):
        # 数据里同时有 <!-- 与 <script：浏览器会把 <!-- 之后的 </script> 当注释里的，脚本整段失效；< 一律转义
        f = Path(self.tmp.name) / "x.html"
        C.write_page("overview.html", {"generated": 1, "hosts": [], "notices": [], "projects": [], "colors": {}, "msg": "<!-- <script> </script>"}, f)
        html = f.read_text()
        self.assertIn('"msg": "\\u003c!-- \\u003cscript> \\u003c/script>"', html)
        self.assertEqual(html.count("<!--"), (ROOT / "labmon" / "overview.html").read_text().count("<!--"))


class CollectorTest(unittest.TestCase):
    def test_fresh_waits_for_round_started_after_request(self):
        # 一轮进行中发起 fresh 的立即采集：进行中的那轮读的是改之前的，要等请求之后才开始的那一轮
        from labmon.serve import _Collector
        seq = []

        class Slow(_Collector):
            interval = 100

            def once(self):
                seq.append(time.time())
                time.sleep(0.3)
        c = Slow()
        threading.Thread(target=c.loop, daemon=True).start()
        time.sleep(0.1)
        t0 = time.time()
        self.assertEqual(c.collect_now(fresh=True, timeout=5), "done")
        self.assertEqual((c.runs, [s > t0 for s in seq]), (2, [False, True]))
        c.last_end = time.time()
        self.assertEqual(c.collect_now(timeout=5), "cached")                    # 刚采过：不采
        c.last_end = 0
        self.assertEqual((c.collect_now(timeout=5), c.runs), ("done", 3))


class ProjectReloadTest(unittest.TestCase):
    def test_bad_config_and_id_change(self):
        # 配置里不是 ConfigError 的错（alerts 写成数字）：只记日志、沿用旧配置，项目线程不退出；登记目录里的 id 被人改了也沿用旧配置（#161：推、页面都改不了 id）
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"LABMON_STATE": td + "/state"}):
            d = Path(td)
            proj = Project(register(write_config(d, d / "proj", d / "out")), interval=3600)
            cfg = PJ.cfg_file("out")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cfg.write_text("alerts = 5\n" + cfg.read_text())              # 顶层（写在文件开头，不落进别的段）
                os.utime(cfg, (time.time() + 2, time.time() + 2))
                proj._reload()
                self.assertEqual(proj.cfg["alerts"]["stale_min"], 5)
                cfg.write_text(cfg.read_text().replace("alerts = 5", 'id = "renamed"'))
                os.utime(cfg, (time.time() + 4, time.time() + 4))
                proj._reload()
            self.assertEqual((proj.id, proj.cfg["id"]), ("out", "out"))
            self.assertIn("沿用旧配置", buf.getvalue())
            self.assertIn("登记目录里的配置 id 改成了 renamed", buf.getvalue())


class AlertLogTest(unittest.TestCase):
    def test_rebalance_time_tail_not_logged(self):
        # #25 审查非阻塞 3：挪法建议句尾的“按 HH:MM 的推演”每轮都变；服务日志判告警变没变时不看它（页面与 wait 照样显示），
        # 免得建议一直在时每个项目每轮往 labmon.log 记一整行
        import contextlib
        import io
        from unittest import mock
        from labmon import serve as SV
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"LABMON_STATE": td + "/state"}):
            d = Path(td)
            proj = Project(register(write_config(d, d / "proj", d / "out")), interval=3600)
            text = "把 o 上 /r/q.txt 的 a5 挪到 t（接在 /r/r.txt 后面）：整批预计从 11-10 02:00 提前到 11-09 20:00（早 6.0 小时）；按 %s 的推演"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), mock.patch.object(proj, "_reload"):
                for hm in ("14:02", "14:04", "14:06"):
                    with mock.patch.object(SV.C, "collect", return_value={"alerts": [{"key": "rebalance:pa", "msg": text % hm, "page": ""}]}):
                        proj.once()
            self.assertEqual(buf.getvalue().count("告警 1 条"), 1, buf.getvalue())


class ParseUntilTest(unittest.TestCase):
    def test_past_month_day(self):
        import datetime as dt
        from labmon.__main__ import parse_until
        now = dt.datetime.now()
        long_ago = (now - dt.timedelta(days=200)).strftime("%m-%d")
        self.assertEqual(dt.datetime.fromtimestamp(parse_until(long_ago)).year, now.year + 1)   # 过了半年以上的月日：算明年（12 月底写 01-05）
        for past in ((now - dt.timedelta(days=3)).strftime("%m-%d"), f"{now.year - 1}-{long_ago}", (now - dt.timedelta(minutes=5)).strftime("%m-%d %H:%M")):
            with self.assertRaises(SystemExit) as c:                                           # 近期已经过去的、写全年份的过去：拒绝，不悄悄变成明年
                parse_until(past)
            self.assertIn("已经过去", str(c.exception))
        soon = (now + dt.timedelta(days=2)).strftime("%m-%d")
        self.assertEqual(dt.datetime.fromtimestamp(parse_until(soon + " 18:00")).strftime("%m-%d %H:%M"), soon + " 18:00")

    def test_minutes_and_relative_days(self):
        """精确到分（Issue #9）：+30m、明天 HH:MM、只写 HH:MM；只写日期的仍取当天结束。"""
        import datetime as dt
        from labmon.__main__ import parse_until
        t0 = time.time()
        self.assertAlmostEqual(parse_until("+30m"), t0 + 1800, delta=5)
        self.assertAlmostEqual(parse_until("+1.5h"), t0 + 5400, delta=5)
        tom = dt.date.today() + dt.timedelta(days=1)
        self.assertEqual(parse_until("明天 18:37"), dt.datetime.combine(tom, dt.time(18, 37)).timestamp())
        self.assertEqual(parse_until(tom.strftime("%m-%d")), dt.datetime.combine(tom, dt.time(23, 59, 59)).timestamp())
        with self.assertRaises(SystemExit) as c:
            parse_until("今天 00:00")                                                         # 今天已经过去的时刻
        self.assertIn("到期时间", str(c.exception))
        with self.assertRaises(SystemExit):
            parse_until("下周")


class HandleErrorTest(unittest.TestCase):
    def test_client_disconnect_not_logged(self):
        # 浏览器关页面时断开连接：不往日志里写 traceback；别的异常照常写
        import contextlib
        import io
        srv = Server.__new__(Server)
        for exc, logged in ((ConnectionResetError(54, "reset"), False), (BrokenPipeError(32, "pipe"), False), (ValueError("坏了"), True)):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                try:
                    raise exc
                except Exception:
                    srv.handle_error(None, ("127.0.0.1", 1))
            self.assertEqual("Traceback" in buf.getvalue(), logged, exc)


class PoolChangeTellTest(unittest.TestCase):
    """共有型算力池新建/修改/删除后自动经 records.tell 通知相关项目实验会话（#249）。"""

    def _post(self, base: str, path: str, tok: str, body: dict):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        )
        try:
            with opener.open(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_pool_save_and_remove_tell_scoped_to_members_and_admins(self):
        from labmon import lease as LS
        from labmon import projects as PJ
        from labmon import records as RC
        from labmon import state as ST
        from fixtures import board, make_token

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            hf = d / "hosts.toml"
            hf.write_text(
                '[[hosts]]\nid = "h1"\nssh = "u@10.0.0.1"\nworker_home = "/data/lab/labmon"\n'
                '[[hosts]]\nid = "h2"\nssh = "u@10.0.0.2"\nworker_home = "/data/lab/labmon"\n'
                '[[hosts]]\nid = "h3"\nssh = "u@10.0.0.3"\nworker_home = "/data/lab/labmon"\n'
            )
            env = {
                "LABMON_STATE": str(d / "state"),
                "LABMON_HOSTS": str(hf),
                "LABMON_NO_NOTIFY": "1",
                "LABMON_NO_KEYCHAIN": "1",
            }
            with mock.patch.dict(os.environ, env):
                srv = board(self, scopes=("admin",), name="boss", role="admin")
                admin_tok = make_token("boss", role="admin", scopes=("admin",))
                make_token("alice", role="user", scopes=("read", "project"))
                make_token("bob", role="user", scopes=("read", "project"))
                base = f"http://127.0.0.1:{srv.server_address[1]}"

                # 登记 5 个项目：无主（归管理员）、boss（管理员）、alice（池成员）、bob（非成员）、alice 已归档项目
                for pid, owner in (
                    ("p_none", None),
                    ("p_boss", "boss"),
                    ("p_alice", "alice"),
                    ("p_bob", "bob"),
                    ("p_arch", "alice"),
                ):
                    PJ.put_config(pid, f'id = "{pid}"\ntitle = "{pid}"\n', owner=owner)
                ST.save_json(ST.state_dir() / "projects.json", {"archived": {"p_arch": {"id": "p_arch", "title": "p_arch", "t": 1}}})

                def clear_tells():
                    tf = RC._tell_file()
                    if tf.exists():
                        tf.unlink()

                def tells_by_proj():
                    out = {}
                    for r in RC.told():
                        for p in r["projects"]:
                            out.setdefault(p, []).append(r)
                    return out

                with mock.patch.object(LS, "primary", return_value=True):
                    # 1. 新建单池（独占模式，members=["alice"]）：入口用 pool，提示改投 pool；非成员 bob 与已归档 p_arch 不收
                    clear_tells()
                    st, out = self._post(base, "/api/pools/save", admin_tok, {
                        "id": "a100", "label": "A100 池", "hosts": ["h1", "h2"],
                        "members": ["alice"], "exclusive": True, "create": True,
                    })
                    self.assertEqual(st, 200, out)
                    got = tells_by_proj()
                    self.assertEqual(set(got), {"p_none", "p_boss", "p_alice"})
                    self.assertNotIn("p_bob", got)
                    self.assertNotIn("p_arch", got)
                    for pid in ("p_none", "p_boss", "p_alice"):
                        rec = got[pid][0]
                        self.assertTrue(rec["key"].startswith(f"page:pool:{pid}:"))
                        self.assertIn("新建了共有算力池「A100 池」", rec["msg"])
                        self.assertIn("入口 pool，", rec["msg"])
                        self.assertIn("h1、h2", rec["msg"])
                        self.assertIn("独占模式", rec["msg"])
                        self.assertIn("改投 pool", rec["msg"])

                    # 2. 无改动编辑：不重复发 tell
                    clear_tells()
                    st, out = self._post(base, "/api/pools/save", admin_tok, {
                        "id": "a100", "label": "A100 池", "hosts": ["h1", "h2"],
                        "members": ["alice"], "exclusive": True, "create": False,
                    })
                    self.assertEqual(st, 200, out)
                    self.assertFalse(out["changed"])
                    self.assertEqual(tells_by_proj(), {})

                    # 3. 新建第二个池（多池、开放模式，members=["bob"]）：入口用 pool:h800，开放模式文案；非成员 alice 不收
                    clear_tells()
                    st, out = self._post(base, "/api/pools/save", admin_tok, {
                        "id": "h800", "label": "H800 池", "hosts": ["h3"],
                        "members": ["bob"], "exclusive": False, "create": True,
                    })
                    self.assertEqual(st, 200, out)
                    got = tells_by_proj()
                    self.assertEqual(set(got), {"p_none", "p_boss", "p_bob"})
                    self.assertNotIn("p_alice", got)
                    self.assertNotIn("p_arch", got)
                    msg = got["p_bob"][0]["msg"]
                    self.assertIn("新建了共有算力池「H800 池」", msg)
                    self.assertIn("入口 pool:h800", msg)
                    self.assertIn("开放模式", msg)
                    self.assertIn("直投池内单机", msg)

                    # 4. 修改 a100（移除 alice、改为开放模式）：
                    #    先模拟 p_alice 在 a100 有待放清单，验证 p_alice 仅收到脱敏调整提示（不泄露新主机/新模式），bob 仍不收
                    clear_tells()
                    ST.save_json(ST.state_dir() / "snapshot.json", {
                        "pool": {"lists": {"a100|/data/lab/p_alice|jobs/q.txt": {
                            "pool": "a100", "project": "p_alice", "root": "/data/lab/p_alice", "jobs": "jobs/q.txt", "done": False,
                        }}},
                        "regs": [],
                    })
                    st, out = self._post(base, "/api/pools/save", admin_tok, {
                        "id": "a100", "label": "A100 池", "hosts": ["h1"],
                        "members": [], "exclusive": False, "create": False,
                    })
                    self.assertEqual(st, 200, out)
                    got = tells_by_proj()
                    self.assertEqual(set(got), {"p_none", "p_boss", "p_alice"})
                    self.assertNotIn("p_bob", got)
                    self.assertIn("修改了共有算力池「A100 池」", got["p_boss"][0]["msg"])
                    self.assertIn("入口 pool:a100", got["p_boss"][0]["msg"])
                    self.assertIn("开放模式", got["p_boss"][0]["msg"])
                    self.assertIn("已调整", got["p_alice"][0]["msg"])
                    self.assertNotIn("开放模式", got["p_alice"][0]["msg"])

                    # 5. 清空快照后再次修改 a100（仍不含 alice）：无待放记录的非成员 alice 与 bob 均绝不收到通知
                    clear_tells()
                    ST.save_json(ST.state_dir() / "snapshot.json", {"pool": {"lists": {}}, "regs": []})
                    st, out = self._post(base, "/api/pools/save", admin_tok, {
                        "id": "a100", "label": "A100 池", "hosts": ["h1", "h2"],
                        "members": [], "exclusive": True, "create": False,
                    })
                    self.assertEqual(st, 200, out)
                    got = tells_by_proj()
                    self.assertEqual(set(got), {"p_none", "p_boss"})
                    self.assertNotIn("p_alice", got)
                    self.assertNotIn("p_bob", got)

                    # 6. 删除 h800（多池中删除，原成员 bob）：仅通知原池成员 bob 与管理员项目，不通知非成员 alice 与已归档项目
                    clear_tells()
                    st, out = self._post(base, "/api/pools/remove", admin_tok, {"id": "h800"})
                    self.assertEqual(st, 200, out)
                    got = tells_by_proj()
                    self.assertEqual(set(got), {"p_none", "p_boss", "p_bob"})
                    self.assertNotIn("p_alice", got)
                    self.assertNotIn("p_arch", got)
                    msg = got["p_bob"][0]["msg"]
                    self.assertIn("移除了共有算力池「H800 池」", msg)
                    self.assertIn("原入口 pool:h800", msg)
                    self.assertIn("h3", msg)

                    # 7. 删除最后剩下的单池 a100（members=[]）：仅通知管理员项目，原入口为 pool
                    clear_tells()
                    st, out = self._post(base, "/api/pools/remove", admin_tok, {"id": "a100"})
                    self.assertEqual(st, 200, out)
                    got = tells_by_proj()
                    self.assertEqual(set(got), {"p_none", "p_boss"})
                    self.assertNotIn("p_alice", got)
                    self.assertNotIn("p_bob", got)
                    self.assertIn("原入口 pool，", got["p_boss"][0]["msg"])


class ResetHistApiTest(unittest.TestCase):
    """POST /api/worker/reset-hist 与 labmon worker reset-hist 的权限、参数校验、共享盘去重与独立主机透传（#300）。"""

    def _post(self, base: str, path: str, token: str, body: dict):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with opener.open(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_reset_hist_api_auth_validation_dedup_and_cli(self):
        import io
        from labmon import __main__ as CLI
        from labmon import client as CL
        from labmon import lease as LS
        from labmon import overview as OV
        from labmon import projects as PJ
        from labmon import workerctl as WC
        from fixtures import board, make_token

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            shared_home = d / "shared_labmon"
            ext_home = d / "ext_labmon"
            hf = d / "hosts.toml"
            hf.write_text(
                f'[[hosts]]\nid = "h1"\nssh = "u@10.0.0.1"\npython = "{sys.executable}"\nworker_home = "{shared_home}"\nusers = ["alice"]\n'
                f'[[hosts]]\nid = "h2"\nssh = "u@10.0.0.2"\npython = "{sys.executable}"\nworker_home = "{shared_home}"\nusers = ["alice"]\n'
                f'[[hosts]]\nid = "ext1"\nssh = "u@10.0.0.3"\npython = "{sys.executable}"\nworker_home = "{ext_home}"\nstandalone = true\nowner = "alice"\n'
                f'[[hosts]]\nid = "h_bob"\nssh = "u@10.0.0.4"\npython = "{sys.executable}"\nworker_home = "{d / "bob_labmon"}"\nusers = ["bob"]\n'
                '[pools.a100]\nlabel = "A100"\nhosts = ["h1", "h2"]\nmembers = ["alice"]\n'
            )
            env = {
                "LABMON_STATE": str(d / "state"),
                "LABMON_HOSTS": str(hf),
                "LABMON_NO_NOTIFY": "1",
                "LABMON_NO_KEYCHAIN": "1",
            }
            with mock.patch.dict(os.environ, env):
                srv = board(self, scopes=("admin",), name="boss", role="admin")
                admin_tok = make_token("boss", role="admin", scopes=("admin",))
                alice_tok = make_token("alice", role="user", scopes=("read", "project"))
                bob_tok = make_token("bob", role="user", scopes=("read", "project"))
                base = f"http://127.0.0.1:{srv.server_address[1]}"

                PJ.put_config("p_alice", 'id = "p_alice"\ntitle = "Alice"\n', owner="alice")
                PJ.put_config("p_bob", 'id = "p_bob"\ntitle = "Bob"\n', owner="bob")

                with mock.patch.object(LS, "primary", return_value=True):
                    # 1. 参数校验：缺 project / tag 与 name 全空 / 多余 args 均返 400
                    st, out = self._post(base, "/api/worker/reset-hist", alice_tok, {"host": "h1", "tag": "heavy"})
                    self.assertEqual(st, 400, out)
                    st, out = self._post(base, "/api/worker/reset-hist", alice_tok, {"host": "h1", "project": "p_alice"})
                    self.assertEqual(st, 400, out)
                    st, out = self._post(base, "/api/worker/reset-hist", alice_tok, {"host": "h1", "project": "p_alice", "tag": "heavy", "args": ["extra"]})
                    self.assertEqual(st, 400, out)

                    # 2. 权限校验：无权项目返 403、无权主机返 403
                    st, out = self._post(base, "/api/worker/reset-hist", bob_tok, {"host": "h_bob", "project": "p_alice", "tag": "heavy"})
                    self.assertEqual(st, 403, out)
                    st, out = self._post(base, "/api/worker/reset-hist", alice_tok, {"host": "h_bob", "project": "p_alice", "tag": "heavy"})
                    self.assertEqual(st, 403, out)

                    # 3. pool 目标共享盘去重（h1 与 h2 共用 shared_home，只调用一次 ssh）
                    calls = []
                    real_ssh = WC.ssh

                    def spy_ssh(h, remote, stdin="", timeout=None):
                        calls.append(h["id"])
                        return real_ssh(dict(h, ssh=""), remote, stdin, timeout)

                    with mock.patch.object(WC, "ssh", side_effect=spy_ssh):
                        st, out = self._post(base, "/api/worker/reset-hist", alice_tok, {
                            "host": "pool", "project": "p_alice", "tag": "heavy", "name": "train_seed0",
                        })
                    self.assertEqual(st, 200, out)
                    self.assertEqual(out["rc"], 0, out)
                    self.assertEqual(len(calls), 1, calls)
                    self.assertIn("任务前缀=train", out["out"])
                    self.assertIn("h1、h2", out["out"])
                    rf = json.loads((shared_home / "hist_reset.json").read_text())
                    self.assertEqual(len(rf["rules"]), 1)
                    self.assertEqual((rf["rules"][0]["project"], rf["rules"][0]["tag"], rf["rules"][0]["name"]),
                                     ("p_alice", "heavy", "train"))

                    # 4. all 目标：管理员对 p_bob 写入，bob 独立/共享目录写入；随后核对 ext1（属 alice）的 _hist_payload 只透传 p_alice 的 resets，不泄露 p_bob
                    with mock.patch.object(WC, "ssh", side_effect=spy_ssh):
                        st, out = self._post(base, "/api/worker/reset-hist", admin_tok, {
                            "host": "all", "project": "p_bob", "tag": "t_bob",
                        })
                    self.assertEqual(st, 200, out)
                    self.assertEqual(out["rc"], 0, out)
                    self.assertTrue((ext_home / "hist_reset.json").is_file())
                    ext_h = {"id": "ext1", "standalone": True, "owner": "alice"}
                    hp = OV._hist_payload(ext_h, [{"id": "p_alice", "lanes": []}, {"id": "p_bob", "lanes": []}], time.time(), {})
                    self.assertIsNotNone(hp)
                    self.assertEqual([r["project"] for r in hp.get("resets") or []], ["p_alice"])

                # 5. CLI 调用与参数校验
                with mock.patch.object(CL, "run", return_value=0) as mrun:
                    rc = CLI.main(["worker", "reset-hist", "pool", "--project", "p_alice", "--tag", "heavy", "--name", "train_s0"])
                    self.assertEqual(rc, 0)
                    mrun.assert_called_once()
                    args_c, _ = mrun.call_args
                    self.assertEqual(args_c[:2], ("POST", "/api/worker/reset-hist"))
                    self.assertEqual(args_c[2], {"host": "pool", "args": [], "project": "p_alice", "tag": "heavy", "name": "train_s0"})
                with self.assertRaises(SystemExit) as cm, mock.patch("sys.stderr", io.StringIO()):
                    CLI.main(["worker", "reset-hist", "h1", "--project", "p_alice"])
                self.assertEqual(cm.exception.code, 2)
