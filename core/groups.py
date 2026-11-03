"""群聊：让几个角色同场说话。

分组依据不是随机挑人，而是角色档案里的「同场角色（按共同出场关卡数）」表
——那是官方剧情里他们一起出现过多少场，本身就是关系强度的度量。
导入器把这张表存成了 data/cooccur.json。

回复调度是简化的：用户发言后，按与「上一位发言者」的同场强度加权，
抽 1~2 个角色接话。不做真正的多轮抢话，那需要独立的状态机。
"""
from __future__ import annotations

import json
import os
import random
import time


class GroupStore:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "groups.json")

    def load(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d.get("groups") or []
        except Exception:
            return []

    def save(self, groups: list[dict]):
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"groups": groups}, f, ensure_ascii=False, indent=1)

    def create(self, name: str, members: list[str]) -> dict:
        g = {
            "id": "g" + str(int(time.time() * 1000))[-9:],
            "name": name.strip() or "未命名群",
            "members": [m for m in members if m],
            "created_at": time.time(),
        }
        gs = self.load()
        gs.append(g)
        self.save(gs)
        return g

    def delete(self, gid: str):
        self.save([g for g in self.load() if g.get("id") != gid])

    def get(self, gid: str) -> dict | None:
        for g in self.load():
            if g.get("id") == gid:
                return g
        return None

    # ------------------------------------------------------------------
    def auto_build(self, cooccur: dict[str, list], cards: list, size: int = 4) -> dict | None:
        """从同场关系里挑关系最密的一群人建群。"""
        name_to_slug = {c.name: c.slug for c in cards}
        slug_to_name = {c.slug: c.name for c in cards}

        # 只保留能在角色库里找到的名字
        graph: dict[str, list[tuple[str, int]]] = {}
        for who, pairs in (cooccur or {}).items():
            if who not in name_to_slug:
                continue
            edges = [(n, c) for n, c in pairs if n in name_to_slug and n != who]
            if edges:
                graph[who] = edges

        if not graph:
            # 没有同场数据：随便找几个角色
            picked = [c.slug for c in cards[:size]]
            if len(picked) < 2:
                return None
            return self.create("角色群", picked)

        # 关系总权重最高的人当中心
        center = max(graph.items(), key=lambda kv: sum(c for _, c in kv[1]))[0]
        near = sorted(graph[center], key=lambda x: -x[1])
        names = [center] + [n for n, _ in near[: size - 1]]
        slugs = [name_to_slug[n] for n in names if n in name_to_slug]

        label = "、".join(names[:2]) + ("等" if len(names) > 2 else "")
        return self.create(f"{label}的群", slugs)

    def pick_speakers(self, group: dict, cooccur: dict, last_speaker: str,
                      rng: random.Random, count: int = 2) -> list[str]:
        """按与上一位发言者的同场强度加权，抽几个接话的人。"""
        members = [m for m in (group.get("members") or []) if m]
        if not members:
            return []
        if len(members) <= count:
            return members[:]

        weights = []
        for slug in members:
            if slug == last_speaker:
                weights.append(2.0)          # 刚说过的人也可能补一句
                continue
            pairs = cooccur.get(slug) or []
            w = 1.0
            for n, c in pairs:
                if n == last_speaker:
                    w += min(c, 20) * 0.25
                    break
            weights.append(w)

        picked, pool = [], list(range(len(members)))
        for _ in range(min(count, len(members))):
            if not pool:
                break
            ws = [weights[i] for i in pool]
            i = rng.choices(pool, weights=ws, k=1)[0]
            pool.remove(i)
            picked.append(members[i])
        return picked
