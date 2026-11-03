# 99陪伴

一个本地优先的 AI 角色陪伴应用骨架。架构参照「星伴 Starlit」逆向分析结果重建，**代码全部自研，不含其任何素材**。

- 角色**全部由你导入**，程序内置零个角色
- 数据只存本机 SQLite，不需要账号
- 模型可换：任何 OpenAI 兼容端点（DeepSeek / OpenAI / Moonshot / 本地 Ollama）

---

## 一、跑起来

1. 双击 `run.bat`（或在目录里 `python server.py`）
2. 浏览器打开 http://127.0.0.1:8787
3. 右上「设置」填 Base URL、API Key、模型名 → 点「测试连接」
4. 左下「导入角色」选一张角色卡

只需要 Python 3.10+。依赖 `requests`、`pillow`（`pip install requests pillow`）。其余全是标准库，没有前端构建步骤。

---

## 二、导入角色

支持三种卡：

| 格式 | 说明 |
|---|---|
| **Tavern Card V2** | `{spec:"chara_card_v2", spec_version:"2.0", data:{...}}`，SillyTavern / RisuAI 生态的通用格式 |
| **PNG 内嵌卡** | 把 JSON 塞在 PNG 的 `tEXt` chunk（keyword=`chara`），直接把图拖进来即可 |
| **V1 裸卡** | `{name, description, personality, first_mes, ...}` |

校验规则：人设必须 ≥ 200 字（要能写出说话方式、性格、边界，太短的卡跑起来一定像个客服机器人）。

示例卡在 `sample-cards/shen-zhixia.card.json`，可以直接导入看效果。

### 卡片字段

```jsonc
{
  "spec": "chara_card_v2", "spec_version": "2.0",
  "data": {
    "name": "沈知夏",                    // 必填
    "description": "她是谁",             // V2 字段，会拼进人格
    "personality": "性格",
    "scenario": "处境",
    "first_mes": "第一句话",
    "mes_example": "对话样本",           // 强烈建议写，能大幅提升语气像不像
    "interests": ["旧书", "雨声"],        // 她自己的兴趣，用于主动搭话
    "tags": ["温和", "慢热"],
    "series": "",                        // 作品来源，原创留空

    // ↓ 本项目扩展（可选）
    "schedule": { "sleep": "00:40", "wake": "08:30",
                  "label": "明天要早起开店" },   // 不填则按名字自动派生
    "traits":   { "enthusiasm": 45, "affection": 55 },
    "extensions": { "accent": "#b9a58c" }        // 主题色
  }
}
```

### 目录形态

导入后每个角色一个目录，**人设是 Markdown，你可以直接手改**：

```
characters/沈知夏/
├── card.json     声明（名字/兴趣/主题色/作息）
├── persona.md    人格正文 ← 改这个最有效，改完刷新页面就生效
├── avatar.png    256×256（可选）
└── state.json    运行时标记
```

`persona.md` 是权威副本：它和 `card.json` 里的 `persona_md` 不一致时，以 `persona.md` 为准。

---

## 三、它和原版怎么对应

| 能力 | 原版做法 | 这里 |
|---|---|---|
| 双通道推理 | analyze（JSON）+ express（生成） | 同，`core/think.py` |
| 三层记忆 | 画像 / 情节 / 情绪，SQLite | 同，`core/db.py` + `core/memory.py` |
| 记忆版本链 | `root_id/previous_id/version/superseded_at` | 同（记忆可被"修正"而非覆盖） |
| 向量召回 | 本地字符 n-gram 哈希 256 维 | 同，`embed()` 一个函数，可整体换真 embedding |
| 事实三态 | candidate/confirmed/conflict | 同，只有 confirmed 进人格 |
| 拟真参数 | 热情/好感 → 频率与延迟 | 同，`core/traits.py`（真实聊天分位标定） |
| 作息 | 未配置按名字派生 | 同，`core/schedule.py` |
| 角色卡 | 私有 spec 1.0 | **Tavern V2 兼容** + 扩展字段 |
| 局域网同步 | 整包备份 + SHA256 | 未实现（自用单机不需要） |
| QQ/OneBot 通道 | 占后端约 1/4 代码 | 未实现（桌面直聊不需要） |

---

## 四、代码结构

```
server.py              HTTP 服务 + 路由 + 聊天主流程（标准库）
core/db.py             SQLite schema（记忆版本链、事实三态、状态）
core/memory.py         embed / cosine / recall / revise（版本链）
core/cards.py          角色卡解析（V2/V1/PNG）、头像规范化
core/traits.py         热情好感 → 主动频率与回复延迟
core/schedule.py       作息派生与睡眠判定
core/think.py          双通道 prompt 组装
core/llm.py            OpenAI 兼容客户端
web/index.html         单页界面（无构建）
characters/            你的角色（导入后生成）
data/companion.db      所有记忆与对话
```

---

## 五、已知限制

- **没有主动性后台循环**。`traits.proactive_interval_minutes()` 已经算好间隔，但没接调度器——现在只有你说话它才回。要开主动消息，在 `server.py` 里加个线程按间隔调 `thinker.has_something_to_say()`，先问"真的有话说吗"再发。
- **没有流式输出**。回复整段返回。要流式得改 `llm.py` 加 `stream=True` 并按 SSE 推。
- **群聊/多角色同场**没做（原版占 118KB 代码，自用价值低）。
- **记忆检索**是词袋哈希，同义改写召回不到。要准就换 embedding——只改 `memory.embed()` 一个函数。

---

## 六、关于素材

`sample-cards/` 里的角色是原创占位。你导入自己的角色时，请注意立绘与人设的版权归属：从商业游戏里提取的角色素材不能用于分发。

---

## 关系阶段（好感度分级）

好感不只是"频率倍率"，它决定**能聊到哪一层**。每一档都写死了可以做什么、还不能做什么：

| 好感 | 阶段 | 分寸 |
|---|---|---|
| 0–29 | 生疏 | 客气、有分寸，不碰感情话题 |
| 30–54 | 熟识 | 聊日常与喜好，试探用玩笑带过 |
| 55–74 | 亲近 | 可以说在意、说想念，但不明说心意 |
| 75–89 | 很亲近 | 有偏心和暧昧的试探，半真半假地撩 |
| 90–100 | 暧昧 | 可以明着撩、撒娇、吃醋 |

两条线始终守住：不写露骨性描写；不写「只有我能救你」这类不健康绑定。

好感随互动质量缓慢上涨（一次最多 +2），也可以在角色面板里手动拉。

## 群聊

左栏顶部那个「+人」按钮会按**同场关系**自动建群——数据来自每个角色档案里的
「同场角色（按共同出场关卡数）」表，那是官方剧情里他们一起出现过多少场。
维尔汀的群自然是十四行诗、红弩箭、星锑。

群里每次会有 1~2 个角色接话，按与上一位发言者的同场强度加权挑选。

## 桌面版

```
dist/99陪伴/99陪伴.exe     ← 双击即可，78MB
```

用 Edge/Chrome 的 `--app` 模式开无地址栏窗口，不背 Electron。
重新打包：

```bat
python -m PyInstaller --noconfirm --clean --onedir --console --name "99陪伴" ^
  --add-data "web;web" launcher.py
```

打完后把 `characters/ data/ stickers/` 复制到 `dist/99陪伴/` 旁边。

## 导入 1999 全量角色

```bat
python tools/import_1999.py            :: 全量（幂等，已存在的跳过）
python tools/rebuild_cooccur.py        :: 只重建同场关系图
```

173 个角色分两档质量：

- **30 个有人物形象卡** → 完整人格卡（含句式统计、口癖、禁忌）+ 13~15 条设定
- **132 个只有档案** → 档案 + 语言特征统计 + **8 句真实台词样本**（比任何总结都准）
- 剩下 12 个资料太少（不足 80 字）未导入
