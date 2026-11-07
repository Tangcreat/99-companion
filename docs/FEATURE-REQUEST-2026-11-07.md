# 功能仿制需求 · 2026-11-07

来源：用户提供了星伴（Starlit）两张截图，要求「再次读取这个软件的各项功能并仿制」。
本文件是主理人调研星伴实现后整理的需求与接口契约，交给工程师实现。

参考实现位置（只读，不要修改）：
- `C:/Users/17807/AppData/Local/Programs/starlit-desktop/resources/webclient/index.html`
- `C:/Users/17807/AppData/Local/Programs/starlit-desktop/resources/webclient/app.js`
- `C:/Users/17807/AppData/Local/Programs/starlit-desktop/resources/webclient/character-presets.js`

我们的项目：`E:/Users/2026-11-03-21-15-33/starlit-clone`

---

## 功能 A：添加角色弹窗（草稿 → 编辑/试聊 → 显式提交）

### 星伴的交互流程

1. 点「添加角色」弹出模态框，标题「添加角色」，副标题「先检查人设和聊感，确认后再让她正式入住」
2. 顶部：圆形头像预览（未选时显示 `?`）+「选择头像」按钮，提示「只使用本地图片，服务端会裁成 256×256；正式添加前必须选择。」
3. 「角色名」输入框（maxlength 24，placeholder「例如：天童爱丽丝」）
4. 两个并排按钮：「导入 JSON 角色卡」「联网生成草稿」
5. 预设行：下拉「选择角色卡预设…」+「下载所选 JSON」按钮；下面一行说明
   「支持星伴、chara_card_v1 / v2 及常见 description / personality / scenario 字段。可先下载模板或预设，修改后导入；也可直接点「使用预设」。」+「使用预设」按钮
6. 状态行：默认「草稿与试聊不会写入正式角色、聊天或记忆。」，操作时变成进度提示
7. **生成/导入成功后展开编辑器**（`characterEditor`）：
   - 作品/来源、别名（顿号或逗号分隔，最多 8 项）
   - 一句话简介（textarea，最多 240 字）
   - 她自己的兴趣（最多 8 项）
   - 完整人设 Markdown（textarea，12 行，最多 12000 字）
   - 底部：`草稿 revision N` +「保存修改」「导出 JSON 角色卡」
8. **创建前试聊区**：
   - 标题「创建前试聊」+ 说明「最多保留 24 条临时上下文，重启即清空」+ 徽章「不写入记忆」
   - 试聊消息列表（空气泡提示「先说一句你好，看看她的语气是否合适。」）
   - 输入框（Enter 发送，Shift+Enter 换行）+「发送」
9. 底部：「取消」「确认添加角色」（初始 disabled，有草稿且已选头像才可点）

### 后端接口契约（与星伴一致）

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| POST | `/api/character-drafts/generate` | `{name, avatar}` | 联网/模型生成草稿，**不创建正式角色** |
| POST | `/api/character-drafts/import` | `{card}` 或 `{name, persona_md, ...}` | 导入 JSON 角色卡为草稿；也用于「使用预设」 |
| PATCH | `/api/character-drafts` | `{draft_id, revision, ...changes}` | 保存修改，revision+1；revision 不匹配要报错 |
| POST | `/api/character-drafts/chat` | `{draft_id, revision, text}` | 试聊：用草稿人设回复，**不写记忆/画像/情绪/聊天表** |
| POST | `/api/character-drafts/commit` | `{draft_id, revision}` | 校验后正式建卡；要求已选头像 |
| GET | `/api/character-drafts` | — | 列出未完成草稿（重开弹窗可恢复） |

草稿存哪：`data/character_drafts.json`（小文件，重启可保留草稿但试聊上下文清空）。
草稿字段：`id / revision / name / series / aliases / summary / interests / persona_md / avatar( dataURL 或已存路径 ) / trial: [{role,text}] / created_at / updated_at`。

### 预设

星伴有 5 个：空白 JSON 模板、青梅竹马、温柔学姐、可靠搭档、犀利编辑。
**要求**：做同样的 5 项结构与入口，但**人设文案必须原创**（不要照抄星伴 persona_md 原文），
每个 400~700 字，含「基本身份 / 性格与边界 / 说话方式 / 相处方式」四节。
放空模板 + 4 个原创预设到 `web/character-presets.js`，暴露 `window.STARLIT_CHARACTER_PRESETS`。

「下载所选 JSON」= 把该预设按 Tavern V2 结构（`{spec:'chara_card_v2', spec_version:'2.0', data:{...}}`）导出下载。
「使用预设」= 走 `/api/character-drafts/import` 载入为草稿，不请求模型。

### 头像裁剪

星伴有独立裁剪弹窗（`avatarMask / avatarStage / avatarCropImg / avatarZoom / avatarRepick / avatarConfirm`），
支持拖动 + 缩放后确认。
**要求**：做一个可用的裁剪弹窗（拖动 + 滚轮/滑杆缩放，圆形预览，输出 256×256 PNG dataURL）。
可以比星伴简化，但必须有「重新选择」「取消」「确认」。

---

## 功能 B：表情包面板

### 星伴的结构（对应截图 1）

```
┌──────────────────────────────────────────────────────────┐
│ [blue archive(217)] [mygo(179)] [stolen(16)] [超时空…(281)] │ ← 分类 tabs，横向滚动，带数量
│                                    [管理] [搜索表情___] [×] │
│ （管理模式下）点 × 删除，点 + 添加图片  [+分类][删分类][+添加] │
│ ┌────┬────┬────┬────┬────┬────┬────┬────┐                │
│ │img │img │img │img │img │img │img │img │  ← 8 列网格      │
│ └────┴────┴────┴────┴────┴────┴────┴────┘                │
└──────────────────────────────────────────────────────────┘
```

要点：
- tabs 显示 `分类名(数量)`；另有「最近使用」分类（非管理模式且有最近记录时显示在最前）
- 「管理」进入管理模式：每个表情右上角出现 ×，可删；出现管理条（+分类 / 删分类 / +添加）
- 搜索框按表情名/标签/情绪过滤当前分类
- 空结果显示「没有找到这个表情…」
- 点表情 = 发送（走现有 `/api/stickers/send`）
- 单张图片 >8MB 跳过并提示

### 后端接口契约

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sticker-sets` | 返回 `{sets: {key: {key,name,count,...}}}`，分类与数量 |
| GET | `/api/stickers` | 返回表情列表（含 `file/name/emotions/tags/url/set`），支持 `?set=` `?q=` 过滤 |
| POST | `/api/stickers/upload` | `{name, data(dataURL), set}` 上传图片到指定分类，>8MB 拒绝 |
| POST | `/api/stickers/delete` | `{file}` 删除单个表情（同时删缩略图与索引项） |
| POST | `/api/sticker-category/create` | `{name}` 新建分类（建目录） |
| POST | `/api/sticker-category/delete` | `{name}` 删分类（空目录才允许，否则报错） |

「最近使用」：前端 localStorage 记录最近发送的 20 个即可，不需要后端。

---

## 我们项目的现状（要改的文件）

| 文件 | 现状 |
|---|---|
| `server.py` | 已有 `/api/characters/generate`、`/api/characters/create`、`/api/characters/import`、`/api/stickers`、`/api/stickers/send`、`/api/avatar/upload` |
| `core/cards.py` | `CardStore.create/import_bytes`，人设最少 200 字（`PERSONA_MIN_CHARS`） |
| `core/persona_builder.py` | `generate()`：给一个名字，模型写人设 JSON（含 lore） |
| `core/sticker.py` | `StickerStore`：扫描目录建索引，按情绪挑图；索引在 `stickers/index.json` |
| `core/think.py` | `Thinker.express(card, state, sched, memories, profile_text, facts_text, analysis, recent, extra_instruction, lore_text, past_intent, user_name)` |
| `web/index.html` | 桌面端，单文件，已有「添加角色」弹窗（简陋版）和表情包面板 |
| `web/mobile.html` | 手机端，单文件，功能更全（表情面板/加号面板/聊天菜单/剧情回顾） |

**两个端都要做**，桌面端优先（用户截图是桌面端）。手机端保持同样的交互结构，网格改 4 列。

## 硬性约束

1. **不要破坏现有接口**：`/api/chat`、`/api/messages`、`/api/characters`、`/api/story`、`/api/diagnostics` 等保持原样
2. 草稿与试聊**绝不能**写 `messages` 表、`memories` 表、`char_state`
3. 正式 `commit` 才创建角色目录与初始状态
4. 预设人设文案必须原创（避免照搬星伴原文）
5. 代码风格与现有一致：后端标准库（`http.server`），前端无框架单文件、内联 SVG 图标、中文注释
6. 后端改完必须 `python -m py_compile` 通过；前端 JS 用 `node --check` 通过
