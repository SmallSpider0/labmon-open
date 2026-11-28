"""多项目 rebalance 全局联合规划单元测试（Issue #289）。"""
from __future__ import annotations

import unittest

from labmon import rebalance as RB

H = 3600
NOW = 1_700_000_000.0


def _offer(
    proj: str,
    src_host: str,
    *,
    src_end_h: float,
    step_h: float,
    n: int,
    free_h: float = 0.0,
    lane: list[str] | None = None,
    idle: bool = False,
    room: dict | None = None,
    local: bool = False,
    queue: str = "q.txt",
    root: str = "/proj",
) -> dict:
    """构造一项 worker offer：原清单排到 NOW + src_end_h*H，每行耗时 step_h*H，挪前 k 行到目标机后在 free_h + k*step_h 完成。"""
    jobs = []
    for k in range(1, n + 1):
        there = NOW + (src_end_h - (k - 1) * step_h) * H
        rest = NOW + (src_end_h - k * step_h) * H
        end = NOW + (free_h + k * step_h) * H
        own = NOW + free_h * H
        jobs.append({
            "host": src_host,
            "root": root,
            "queue": queue,
            "name": f"{proj}_j{n - k + 1}",
            "there": there,
            "rest": rest,
            "end": end,
            "own": own,
            "dep": False,
        })
    return {
        "project": proj,
        "lane": lane,
        "before": None,
        "free": NOW + free_h * H,
        "ends": {f"{src_host}|{root}|{queue}": NOW + src_end_h * H},
        "jobs": jobs,
        "why": None,
        "stop": None,
        "idle": idle,
        "room": room,
        "local": local,
    }


def _host(
    hid: str,
    offers: list[dict],
    *,
    ngpus: int = 1,
    running: list[dict] | None = None,
    todo: list[dict] | None = None,
    share: dict | None = None,
    overrides: list[dict] | None = None,
    borrow_late: list[dict] | None = None,
    t: float = NOW,
) -> dict:
    st: dict = {
        "t": t,
        "offers": offers,
        "info": {"gpus": [{"i": i, "uuid": f"GPU-{hid}-{i}"} for i in range(ngpus)]},
        "running": list(running or []),
        "todo": list(todo or []),
    }
    if share is not None:
        st["share"] = share
    if overrides is not None:
        st["overrides"] = overrides
    e: dict = {"id": hid, "label": hid, "worker_up": True, "status": st}
    if borrow_late:
        e["borrow"] = {"late": borrow_late}
    return e


class GlobalRebalanceTest(unittest.TestCase):
    def test_single_project_unchanged(self):
        """单项目场景与原行为完全一致（零退化）。"""
        t1 = _host("t1", [_offer("pa", "o1", src_end_h=10, step_h=2, n=3, idle=True)])
        out = RB.suggest([t1], None, NOW)
        self.assertIn("pa", out)
        s = out["pa"]
        self.assertEqual(s["target"], "t1")
        # 挪 2 行：rest=6h, end=4h -> new=6h；挪 3 行：rest=4h, end=6h -> new=6h；选行少的 2 行
        self.assertEqual(len(s["jobs"]), 2)
        self.assertEqual(s["from"], NOW + 10 * H)
        self.assertEqual(s["to"], NOW + 6 * H)

    def test_focus_project_wins_idle_host_and_other_backs_off(self):
        """两项目同瞄 1 卡空机：有 focus 的项目优先拿满该机，低优项目因扣减后不再满足门槛而退让不报。"""
        # 两项目原处各 8h，每行 3h，挪 1 行到 t1 需 3h（原处剩 5h -> new=5h，早 3h >= 门槛 1h）
        # 当高优 pb 挪 1 行占掉 3h 后，pa 挪 1 行到 t1 变为 end=3h+3h=6h，原处剩 5h -> new=6h（早 2h）；
        # 若 pb 挪 2 行占掉 6h（src_end=10h, step=3h -> 挪 2 行 rest=4h, end=6h -> new=6h），
        # pa 原处 8h，挪 1 行到 t1 后 end=6h+3h=9h > 8h（恶化被拒）-> pa 不报撞车建议！
        o_pa = _offer("pa", "o1", src_end_h=8, step_h=3, n=2, idle=True)
        o_pb = _offer("pb", "o2", src_end_h=10, step_h=3, n=2, idle=True)
        t1 = _host("t1", [o_pa, o_pb], ngpus=1)
        sw = {"focus": {"pb": None}, "weight": {}, "owner": {}}
        out = RB.suggest([t1], None, NOW, sw=sw)
        self.assertEqual(out["pb"]["target"], "t1")
        self.assertEqual(len(out["pb"]["jobs"]), 2)
        self.assertNotIn("pa", out)

    def test_weight_priority_and_spillover_to_second_host(self):
        """两项目同瞄 1 卡空机 t1 且存在第二台空机 t2：高权重项目拿 t1，低优项目自动分流到 t2。"""
        # pb 权重 4，pa 权重 1；t1 对两者都更快（free=0h），t2 稍慢（free=1h）
        t1 = _host(
            "t1",
            [
                _offer("pa", "o1", src_end_h=10, step_h=3, n=2, free_h=0, idle=True),
                _offer("pb", "o2", src_end_h=10, step_h=3, n=2, free_h=0, idle=True),
            ],
            ngpus=1,
            share={"use": {"pa": 2.0, "pb": 2.0}, "weight": {"pa": 1, "pb": 4}},
        )
        t2 = _host(
            "t2",
            [
                _offer("pa", "o1", src_end_h=10, step_h=3, n=2, free_h=1, idle=False),
                _offer("pb", "o2", src_end_h=10, step_h=3, n=2, free_h=1, idle=False),
            ],
            ngpus=1,
        )
        sw = {"weight": {"pa": 1, "pb": 4}, "focus": {}, "owner": {}}
        out = RB.suggest([t1, t2], None, NOW, sw=sw)
        # pb（use/w = 0.5）优先于 pa（use/w = 2.0）：pb 拿 t1（6h），pa 看到 t1 被占 6h 后自动分流到 t2（7h）
        self.assertEqual(out["pb"]["target"], "t1")
        self.assertEqual(out["pa"]["target"], "t2")
        self.assertEqual(out["pa"]["to"], NOW + 7 * H)

    def test_cold_start_weight_tiebreaker(self):
        """冷启动无历史用量（use=0）时，fair_key 相同，权重更高者优先。"""
        o_pa = _offer("pa", "o1", src_end_h=8, step_h=3, n=2)
        o_pb = _offer("pb", "o2", src_end_h=10, step_h=3, n=2)
        t1 = _host("t1", [o_pa, o_pb], ngpus=1)
        sw = {"weight": {"pa": 1, "pb": 3}, "focus": {}, "owner": {}}
        out = RB.suggest([t1], None, NOW, sw=sw)
        self.assertIn("pb", out)
        self.assertNotIn("pa", out)

    def test_person_use_fair_order(self):
        """不同人之间按个人近期总用量（person_use）从小到大排序，低用量者的项目优先。"""
        # Alice 名下 pa1(use=10) + pa2(use=1) -> 个人总用量 11；Bob 名下 pb(use=4) -> 个人总用量 4
        # 即使 pa2 单项目用量(1) < pb(4)，但按人算 Bob(4) < Alice(11)，因此 pb 优先拿到 t1
        o_pa2 = _offer("pa2", "o1", src_end_h=8, step_h=3, n=2)
        o_pb = _offer("pb", "o2", src_end_h=10, step_h=3, n=2)
        t1 = _host("t1", [o_pa2, o_pb], ngpus=1, share={"use": {"pa1": 10.0, "pa2": 1.0, "pb": 4.0}, "weight": {}})
        sw = {"owner": {"pa1": "alice", "pa2": "alice", "pb": "bob"}, "weight": {}, "focus": {}}
        out = RB.suggest([t1], None, NOW, sw=sw)
        self.assertIn("pb", out)
        self.assertNotIn("pa2", out)

    def test_idle_and_room_cleared_after_high_priority_takes_host(self):
        """目标机原本 idle / room 被高优项目占掉后，低优项目不再按 idle 或 room 放宽门槛误报。"""
        # 场景 A：idle 放宽门槛（整批被原处另一条长清单撑到 20h 无法提前，单项目靠 idle 按行比成立）
        o_pb = _offer("pb", "o2", src_end_h=10, step_h=3, n=2, idle=True)
        o_pa_idle = _offer("pa", "o1", src_end_h=8, step_h=2, n=1, idle=True)
        o_pa_idle["ends"]["o1|/proj|long.txt"] = NOW + 20 * H  # 撑住整批 f=20h，只能走 _idle_gain
        t_idle = _host("t_idle", [o_pa_idle, o_pb], ngpus=1)
        out_idle = RB.suggest([t_idle], None, NOW, sw={"focus": {"pb": None}})
        self.assertEqual(out_idle["pb"]["target"], "t_idle")
        self.assertNotIn("pa", out_idle)

        # 场景 B：room 放宽门槛（1 卡机 room.lanes=1, busy=20%；pa 整批只早 25 分钟 < _need，原本靠 room 放宽到 15 分钟）
        o_pb2 = _offer("pb", "o2", src_end_h=10, step_h=2, n=2, room={"busy": 20.0, "lanes": 1})
        o_pa_room = _offer("pa", "o1", src_end_h=10, step_h=0.5, n=1, room={"busy": 20.0, "lanes": 1})
        t_room = _host("t_room", [o_pa_room, o_pb2], ngpus=1)
        # 验证单项目时 pa 确实会靠 room 报出
        solo_pa = RB.suggest([_host("t_room", [o_pa_room], ngpus=1)], None, NOW)
        self.assertIn("pa", solo_pa)
        self.assertTrue(solo_pa["pa"].get("loose"))
        # 联合规划时 pb 拿走唯一 room 槽位，pa 的 room 扣至 0 且平移 4h，不再误报
        out_room = RB.suggest([t_room], None, NOW, sw={"focus": {"pb": None}})
        self.assertEqual(out_room["pb"]["target"], "t_room")
        self.assertNotIn("pa", out_room)

    def test_multi_gpu_host_allows_parallel_projects_up_to_cap(self):
        """2 卡空机允许前 2 个项目各自占 1 张卡不互相阻塞，第 3 个项目才看到排队平移。"""
        offers = [
            _offer("pa", "o1", src_end_h=10, step_h=3, n=2, idle=True),
            _offer("pb", "o2", src_end_h=10, step_h=3, n=2, idle=True),
            _offer("pc", "o3", src_end_h=8, step_h=3, n=2, idle=True),
        ]
        t2g = _host("t2g", offers, ngpus=2)
        out = RB.suggest([t2g], None, NOW)
        # 无 share 时按项目 ID 排序：pa, pb, pc
        # pa 占槽 0 (6h)，pb 占槽 1 (6h)，pc 看到 min(slots)=6h -> end=9h > 8h 被拒
        self.assertEqual(out["pa"]["target"], "t2g")
        self.assertEqual(out["pb"]["target"], "t2g")
        self.assertNotIn("pc", out)

    def test_fallback_to_project_id_without_share_or_sw(self):
        """无 share 与 sw 时稳定回退按项目 ID 排序。"""
        o_pa = _offer("pa", "o1", src_end_h=10, step_h=3, n=2)
        o_pb = _offer("pb", "o2", src_end_h=8, step_h=3, n=2)
        t1 = _host("t1", [o_pb, o_pa], ngpus=1)
        out = RB.suggest([t1], None, NOW)
        self.assertIn("pa", out)
        self.assertNotIn("pb", out)

    def test_stick_hysteresis_yields_when_prev_target_filled(self):
        """上一轮目标机 t1 被高优项目占满后，低优项目不会因 STICK_S 强粘 t1，而是改选 t2。"""
        t1 = _host(
            "t1",
            [
                _offer("pa", "o1", src_end_h=8, step_h=3, n=2),
                _offer("pb", "o2", src_end_h=10, step_h=3, n=2),
            ],
            ngpus=1,
        )
        t2 = _host(
            "t2",
            [_offer("pa", "o1", src_end_h=8, step_h=3, n=2, free_h=0.5)],
            ngpus=1,
        )
        prev = {"pa": {"key": "rebalance:pa:t1:o1/q.txt/pa_j2", "target": "t1", "local": False}}
        out = RB.suggest([t1, t2], prev, NOW, sw={"focus": {"pb": None}})
        self.assertEqual(out["pb"]["target"], "t1")
        self.assertEqual(out["pa"]["target"], "t2")

    def test_borrow_cut_compatible_with_spillover(self):
        """两项目同时被借用截止挡住时，高优优先选 t1，低优自动分流到 t2，各自保留 cut 字段。"""
        o_host = _host(
            "o_guest",
            [],
            todo=[
                {"project": "pa", "root": "/proj", "queue": "qa.txt", "jobs": ["pa_j1", "pa_j2"]},
                {"project": "pb", "root": "/proj", "queue": "qb.txt", "jobs": ["pb_j1", "pb_j2"]},
            ],
            borrow_late=[
                {"root": "/proj", "queue": "qa.txt"},
                {"root": "/proj", "queue": "qb.txt"},
            ],
        )
        t1 = _host(
            "t1",
            [
                _offer("pa", "o_guest", src_end_h=10, step_h=3, n=2, queue="qa.txt"),
                _offer("pb", "o_guest", src_end_h=10, step_h=3, n=2, queue="qb.txt"),
            ],
            ngpus=1,
        )
        t2 = _host(
            "t2",
            [_offer("pa", "o_guest", src_end_h=10, step_h=3, n=2, free_h=1, queue="qa.txt")],
            ngpus=1,
        )
        out = RB.suggest([o_host, t1, t2], None, NOW, sw={"focus": {"pb": None}})
        self.assertEqual(out["pb"]["target"], "t1")
        self.assertEqual(out["pb"]["cut"], ["o_guest|/proj|qb.txt"])
        self.assertEqual(out["pa"]["target"], "t2")
        self.assertEqual(out["pa"]["cut"], ["o_guest|/proj|qa.txt"])

    def test_local_lane_move_does_not_reserve_target_slot(self):
        """同机挪到另一条已有清单（local=True, lane!=None）不新增车道，不扣减目标机对其他项目的余量。"""
        o_pb_local = _offer("pb", "t1", src_end_h=10, step_h=3, n=2, lane=["/proj", "fast.txt"], local=True)
        o_pa_cross = _offer("pa", "o1", src_end_h=8, step_h=3, n=2)
        t1 = _host(
            "t1",
            [o_pb_local, o_pa_cross],
            ngpus=1,
            todo=[{"project": "pb", "root": "/proj", "queue": "q.txt", "jobs": ["pb_j1", "pb_j2"]}],
        )
        out = RB.suggest([t1], None, NOW, sw={"focus": {"pb": None}})
        self.assertEqual(out["pb"]["target"], "t1")
        self.assertTrue(out["pb"]["local"])
        # pb 只是同机挪到已有清单，t1 未被额外占用，pa 仍可正常挪入 t1
        self.assertEqual(out["pa"]["target"], "t1")

    def test_reverse_direction_suppressed_within_cooldown(self):
        """#297：A→B 发出后 30 分钟内（含中间有一轮无建议）抑制反向 B→A，超过 REV_COOL_S 后恢复。"""
        hb = _host("B", [_offer("pa", "A", src_end_h=10, step_h=2, n=3, idle=True)])
        out1 = RB.suggest([hb], None, NOW)
        self.assertEqual(out1["pa"]["target"], "B")
        self.assertEqual(out1["pa"]["dir"], {"src": ["A"], "dst": "B", "t": NOW})

        # 中间一轮风平浪静无建议：dir 仍延续保留，且不含 key/text/why
        out_quiet = RB.suggest([_host("A", []), _host("B", [])], out1, NOW + 300)
        self.assertNotIn("key", out_quiet.get("pa", {}))
        self.assertEqual(out_quiet["pa"]["dir"]["dst"], "B")

        # 10 分钟后出现反向 B→A：被抑制（无 key/text），dir 仍保留
        ha = _host("A", [_offer("pa", "B", src_end_h=10, step_h=2, n=3, idle=True)])
        out2 = RB.suggest([ha], out_quiet, NOW + 600)
        self.assertNotIn("key", out2.get("pa", {}))
        self.assertNotIn("text", out2.get("pa", {}))
        self.assertEqual(out2["pa"]["dir"]["dst"], "B")

        # 超过 REV_COOL_S（30 分钟）后反向 B→A 正常放行
        out3 = RB.suggest([ha], out2, NOW + RB.REV_COOL_S)
        self.assertEqual(out3["pa"]["target"], "A")
        self.assertEqual(out3["pa"]["dir"], {"src": ["B"], "dst": "A", "t": NOW + RB.REV_COOL_S})

    def test_reverse_cooldown_allows_non_reverse_local_and_borrow_cut(self):
        """#297：冷却期内非反向（A→C）、同机挪法（local=True）及 borrow_cut 紧急抢救均不受抑制。"""
        hb = _host("B", [_offer("pa", "A", src_end_h=10, step_h=2, n=3, idle=True)])
        out1 = RB.suggest([hb], None, NOW)
        self.assertEqual(out1["pa"]["target"], "B")

        # 非反向：B→C 允许
        hc = _host("C", [_offer("pa", "B", src_end_h=10, step_h=2, n=3, idle=True)])
        out_c = RB.suggest([hc], out1, NOW + 300)
        self.assertEqual(out_c["pa"]["target"], "C")

        # 同机挪法：B 上同机多开不受跨主机反向冷却影响，且透传原 dir
        hb_local = _host(
            "B",
            [_offer("pa", "B", src_end_h=10, step_h=2, n=3, local=True)],
            todo=[{"project": "pa", "root": "/proj", "queue": "q.txt", "jobs": ["pa_j1", "pa_j2", "pa_j3"]}],
        )
        out_loc = RB.suggest([hb_local], out1, NOW + 300)
        self.assertEqual(out_loc["pa"]["target"], "B")
        self.assertTrue(out_loc["pa"]["local"])
        self.assertEqual(out_loc["pa"]["dir"]["dst"], "B")

        # borrow_cut 紧急抢救：B 马上要还卡，允许立即反向 B→A
        hb_late = _host(
            "B",
            [],
            todo=[{"project": "pa", "root": "/proj", "queue": "q.txt", "jobs": ["pa_j1", "pa_j2", "pa_j3"]}],
            borrow_late=[{"root": "/proj", "queue": "q.txt"}],
        )
        ha_cut = _host("A", [_offer("pa", "B", src_end_h=10, step_h=2, n=3, idle=True)])
        out_cut = RB.suggest([hb_late, ha_cut], out1, NOW + 300)
        self.assertEqual(out_cut["pa"]["target"], "A")
        self.assertEqual(out_cut["pa"]["cut"], ["B|/proj|q.txt"])

    def test_reverse_suppression_does_not_block_other_projects_in_joint_plan(self):
        """#297 × #289：高优项目反向 B→A 被抑制时，不占用 A 的槽位，低优项目仍可正常挪入 A。"""
        hb = _host("B", [_offer("pb", "A", src_end_h=10, step_h=3, n=2, idle=True)])
        prev = RB.suggest([hb], None, NOW)
        self.assertEqual(prev["pb"]["target"], "B")

        ha = _host(
            "A",
            [
                _offer("pb", "B", src_end_h=10, step_h=3, n=2, idle=True),
                _offer("pa", "C", src_end_h=8, step_h=3, n=2, idle=True),
            ],
            ngpus=1,
        )
        out = RB.suggest([ha], prev, NOW + 300, sw={"focus": {"pb": None}})
        self.assertNotIn("key", out.get("pb", {}))
        self.assertEqual(out["pa"]["target"], "A")


if __name__ == "__main__":
    unittest.main()
