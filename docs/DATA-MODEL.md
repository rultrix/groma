# 数据模型与发现树（v0.3）

> **文档版本** v0.3 ｜ **最近更新** 2026-11-09 ｜ **对应 Groma** v0.40.0 ｜ **类型** 现状描述
> v0.3 实质改动两处：① §4.2 加了规则 3.5（子 Agent 类型与父会话写在**路径**里，归因不必读正文），并把
>    2026-10-29 那句「子 Agent 归属必须走 DB 源」收窄成它真正证明过的范围；② §5 不变量 4 为内置子 Agent
>    开了例外（不绑 ws_ref），代价与可推翻性写在原地。
> v0.2 唯一实质改动：把"会话正文从不入库"限定为"**不进入可上传产物与本机索引层**"。
> 本机正文归档（vault）是定稿但未实现的新层，见 [CONTEXT-VAULT.md](CONTEXT-VAULT.md)；
> `session_key` 与 `profile` 两个字段也以那份为准，本文件的身份模型不含它们。

> 状态：**已实现**。§2 存储分层、§3 字段、§4 归属、§5.1 的 qoder `local.db` 双源均已落地。
> 实测（ctx_parser 工作区）：75 个会话，其中 48 个只有数据库记录；子 Agent `qoder-ctx-parser`
> 归到 5 个会话 / 185 轮。
> 依据：`acpe-agent-package` 的 `identity_resolver.py`（schema 0.3）与 `database.py` 的 agents 表；
> 以及本机实测的 qoder/codex/claude 会话字段。

---

## 1. 术语对齐

| 产品词 | 本仓存储 | package 定义 | 说明 |
|---|---|---|---|
| **工作区** | **Unit** | **Workspace = Context Scope** | 抽取 context、build asset 的作用域；**不是 Agent** |
| Harness | source | Harness instance | 产生会话的工具环境 |
| Agent | Agent | Default Agent / Sub Agent | 有身份的对象；子 Agent 有 `parent_agent_id` |
| `ws_ref` | `workspace_ref` | `sha256(路径)` 前若干位 | **脱敏引用**，不是路径；本机另存映射 |

**结论：一个工作区 = 一个 Unit。** 不同 harness、不同 agent 的会话在这里汇聚。

---

## 2. 存储分层

```
全局（跟机器走，只放身份与路径）
%LOCALAPPDATA%\groma\state\
  ├── harnesses.json     harness 数据根
  ├── agents.json        agent 身份（含 parent / ws_ref / 状态）
  ├── units.json         Unit 实体本身：unit_id / name / org_path / home_binding
  ├── memberships.json   unit_id ↔ ws_ref 的关系（并入/解除只改这里）
  └── workspaces.json    ws_ref → 本地真实路径（含 unit_id / org_path）

工作区（跟项目走，放证据与资产）
<workspace>\
  ├── groma-unit.json       Unit 身份的导出件（家目录持有；可提交，让另一台机器认回同一 Unit）
  └── .groma\
      ├── unit.json      本机元数据、ws_ref
      ├── selection.json 本工作区启用的 Default Agent
      ├── scans/ digests/ 采集证据
      └── assets/         build 出来的 context asset（写在 Unit 的家里）
```

**Unit 是实体，不是目录的投影**

| 字段 | 存在哪 | 作用 | 是否可上传/提交 |
|---|---|---|---|
| `unit_id` | `units.json`（权威）＋ `groma-unit.json`（导出件）＋ `.groma/unit.json`（本机镜像） | 跨机稳定的 Unit 身份，`unit_` + 16 hex，不含路径 | 卡片可提交 |
| `name` / `org_path` | `units.json`（权威）→ 改名/改组织路径对该 Unit 的每个目录一起生效 | 人看的名字；`host/owner/repo` 组织统一路径 | 卡片可提交 |
| `home_binding` | `units.json` | 这个 Unit 的**家**：资产、导出、采集设置落在哪个工作区。有且只有一个 | **不提交、不上传** |
| `ws_ref` | `.groma/unit.json`、证据、资产 | 本机脱敏引用（路径 hash），只在本机有意义 | 可上传（不含路径） |
| 本地路径 | `workspaces.json` | `ws_ref → 本机路径` 的解析 | **不提交、不上传** |

解析方式：

```text
unit_id ──► units.json ──► name / org_path / home_binding（家）
        ├─► memberships.json ──► ws_ref ──► workspaces.json ──► 本机路径
        └─► groma-unit.json（卡片）──► 启动时 sync_units() 折叠成上面两条（同一仓库多 clone 自动归位）
```

- 新工作区 `init` 生成 `groma-unit.json` 并登记进本机注册表；`groma unit link --org host/owner/repo` 改的是 **Unit** 的组织路径。
- 把 `groma-unit.json` 提交进仓库后，别的机器 clone 下来 `groma init` 会复用同一个 `unit_id`，本地路径另行登记。
- 卡片只在**家**由 Groma 写入；`并入` 不往成员目录写任何东西。记录建立后，`leave` 不会被旧卡片复活（卡片只在首次导入时折叠）。

**Unit 作用域的读侧（概览 / 发现 / 关系图 / 活动 / 资产）**

页面先问"当前哪个 Unit"，不再问"当前哪个目录"。每个工作区的 digest 只收录 `cwd` 落在它内部的会话，所以 Unit 的并集不是把同一批会话数两遍：

```text
unit_workspaces = 家 ∪ memberships 里并入的目录（units.json + memberships.json 说话，卡片不参与解析）
会话按 (source, path) 去重；成员目录还没采集过就显示"未采集"，不假装有关系
```

- 界面没有"打开工作区＝切换作用域"这个动作了：切换 Unit 用页头切换器；目录行只剩「设为家 / 移出 Unit / 从本机移除」。
- 「家」是 Unit 的一个属性，不跟随鼠标：换家时原来的家自动留为成员，移除家会自动落到还剩下的目录。

**不变量**

1. 同一个 Agent 在任何工作区都是**同一个 `agent_id`** → 身份必须全局；
2. 删掉工作区的 `.groma/` = 删掉证据，全局身份仍在；
3. 子 Agent 的 `parent_agent_id` 必须指向同 harness 的 Default Agent；
4. 子 Agent **必须**绑定一个 `ws_ref`；Default Agent 不绑定（可跨工作区）。
   **2026-11-09 开了一个例外**：harness 自己在文件名里命名的**内置子 Agent**（§4.2 规则 3.5）不绑 `ws_ref`
   ——它由 harness 命名、不属于任何工作区，绑了就会让同一个内置类型在每个工作区长出一个新 `agent_id`，
   违反不变量 1。代价要写明：这类记录 `workspace_ref = None`、`workspace_refs = []`、`config_rel_path = None`，
   所以"按工作区列子 Agent"的界面**不能假设这三个字段非空**。这条例外是本刀新立的，可以被推翻；
   推翻它就等于回到"内置子 Agent 归 Default Agent"那个已实测存在的错归因。
5. 一个 Unit 有且只有一个家；`并入` 与 `解除` 都不写成员目录里的任何文件；
6. 移除最后一个本机工作区会被拒绝——不留没有证据落点的 Unit。

---

## 3. 字段定义

### `harnesses.json`（全局）

```json
{
  "schema": 1,
  "harnesses": [
    {"name": "qoder", "harness": "qoder-cn", "root": "C:/Users/me/.qoder-cn", "include": ["projects"], "added_at": "..."}
  ]
}
```

### `agents.json`（全局）

```json
{
  "schema": 1,
  "agents": [
    {
      "agent_id": "agt-…",
      "name": "Qoder Default Agent",
      "harness": "qoder-cn",
      "kind": "primary",
      "parent_agent_id": null,
      "workspace_ref": null,
      "workspace_refs": [],
      "config_rel_path": null,
      "config_hash": null,
      "verification": "SOURCE_VERIFIED",
      "first_seen": "…",
      "last_seen": "…"
    },
    {
      "agent_id": "agt-…",
      "name": "reviewer",
      "harness": "qoder-cn",
      "kind": "sub",
      "parent_agent_id": "agt-…",
      "workspace_ref": "a286beaee32b2e54",
      "workspace_refs": ["a286beaee32b2e54"],
      "config_rel_path": ".qoder/agents/reviewer.md",
      "config_hash": "…",
      "verification": "DISCOVERED",
      "first_seen": "…",
      "last_seen": "…"
    }
  ]
}
```

- `agent_id` 格式沿用 `agt-<hex>`，**由本仓首次发现时生成并持久化**；
- 外部键（用于与 package 对齐）= `harness:default` 或 `harness:subagent:<ws_ref>:<rel_path>:<name>`；
- `workspace_ref` 只存哈希；真实路径在 `workspaces.json`。

### `workspaces.json`（全局）

```json
{"schema": 1, "workspaces": [{"ref": "ws-a286beaee32b2e54", "path": "<LOCAL-WORKSPACE-PATH>", "name": "ctx-parser", "unit_id": "unit_26a70f876481c77f", "org_path": "github.com/example/upstream-harness", "first_seen": "…", "last_seen": "…"}]}
```

### `groma-unit.json`（工作区根，可提交）

```json
{"schema": 1, "unit_id": "unit_26a70f876481c77f", "name": "ctx-parser", "org_path": "github.com/example/upstream-harness", "created_at": "…", "note": "commit this file; clones on other machines resolve to the same Unit"}
```

### `unit.json`（工作区）

```json
{"schema": 1, "unit_id": "unit_…", "name": "ctx-parser", "workspace_root": "<LOCAL-WORKSPACE-PATH>", "created_at": "…", "carrier": "local-directory"}
```

### `selection.json`（工作区）

```json
{"schema": 1, "enabled_agents": ["agt-qoder-default", "agt-codex-default"], "updated_at": "…"}
```

- **只列 Default Agent**；子 Agent 只要绑定在本工作区就自动参与，不单独选；
- 一个 Agent 在 A 工作区启用、B 工作区不启用，是允许的。

---

## 4. 发现与归属

### 4.1 发现（三步，全部只读）

1. **枚举 harness**：已知根目录（qoder-cn / codex / claude / cursor），只统计元数据；
2. **发现子 Agent**：扫描**本工作区**的 `.qoder/agents/*.md`，取 frontmatter `name`；
   `agent_id` 键 = `harness + ws_ref + 相对配置路径 + name`；
3. **验证**：会话里出现过该名字 → `SOURCE_VERIFIED`；只有配置 → `DISCOVERED`。

> 标准版 Qoder、Claude Code 的子 Agent 本轮 **UNSUPPORTED**——**这句已经过期**，见下面 §4.2 的
> 2026-11-09 更正；Codex 只有 `is_subagent` 标记、**没有名字**，这一半仍然成立。

### 4.2 归属（会话 → Agent）

| 顺序 | 规则 |
|---|---|
| 1 | `cwd` 不在本工作区 → **不归属**（记 `unattributed_outside`） |
| 2 | 会话没有 `cwd` → **不归属**（记 `unattributed_no_cwd`） |
| 3 | qoder 且 **`local.db` 的 `chat_record.extra.key_sub_agent_type`** 命中本区某个子 Agent → 归该子 Agent |
| 3.5 | **（新增）** harness 在文件名里点名了子 Agent：`<父会话 uuid>/subagents/agent-a<type>-<16hex>.jsonl`
  → 按 `type` 归一个**内置子 Agent** 身份（键 = `harness:subagent:builtin:<type>`，跨工作区同一个）；
  Claude 的文件名里**没有 type**（`agent-a<16hex>`），所以只记 `is_subagent` 与父，**不编名字** |
| 4 | codex 且 `is_subagent=true` → 记「未命名子会话」，归 Default Agent 并打标 |
| 5 | 其余 → 归对应 harness 的 **Default Agent** |

> **实测事实（2026-10-29）**：`key_sub_agent_type` 在 JSONL 里**只出现在
> `message.content[].text` 的文本中**，不是结构化字段——读它等于解析对话正文，我们不做。
> 数据库 `chat_record.extra` 里才是结构化字段。
> 本机 qoder 的 `local.db` 有 10 个子 Agent 会话，但它们**没有任何对应的 JSONL transcript**
> （交集 0）。所以**子 Agent 归属必须走 DB 源**；只读 JSONL 时，子 Agent 只能列出来（DISCOVERED）、
> 会话归 Default。
>
> **2026-11-09 更正，这句的结论比当时以为的更窄**：那条实测只说了"**内容**取不到名字"，
> 但**父会话与子 Agent 类型写在路径里**，而路径是我们本来就读的元数据——归因因此不需要碰正文。
> 家仓最新 digest 实测 125 行里 **4 行**是这个形状（qoder `subagents/agent-ageneral-purpose-<hex>.jsonl`），
> 加上 2 行"DB 报了名字但本区没配置"（如 `CodeReview`），**共 6 行原先被算进 Default Agent 名下**：
> `qoder-cn Default Agent` 的会话数 71 → 65，`agent_kind` 分布 primary 120 / sub 5 → **114 / 11**。
> **`file_count` 125 与 `event_count` 120869 一字未动**（前后各跑一遍同一份真实数据对比过）——
> 这次改的是"谁干的"，不是"干了多少"，界面那个"会话 N"也不该因此变化。
>
> **但上面那组数只在"用新代码现跑"时成立，今天盘上的产物还没变**。实测（2026-11-09）：
> 家仓正在用的 `digest-20261109T064420Z.json` 里 **0/125 行有 `is_subagent` 字段**，那 4 行仍挂在
> `qoder-cn Default Agent` 名下，`build_graph` 因此前后都是 3 个 Agent 盒子、71 个会话。
> 原因是产物由**安装包那份构建**产出，而它早于这次改动。生效要三件事都发生，缺一件就还是旧图：
> ① 新代码进构建（不只是进仓库）；② 重跑 catchup 让 digest 重写；③ 读的是重写后的那份。
> 引用本节任何"已修"的结论时，先确认这三件——这条就是 `docs/RULES.md` 里"落码 ≠ 产物"的具体实例。

**同名跨工作区不合并**：`reviewer @ ctx_parser` 与 `reviewer @ raee` 是两个 Agent。
**内置子 Agent 是另一回事**：它由 harness 自己命名、不属于任何工作区，所以跨工作区**应当**是同一个身份
（§5 不变量 4 为此开了例外，见那里）。

---

## 5. 能力矩阵（本轮实测）

| Harness | 命名子 Agent | 会话归属子 Agent | 说明 |
|---|---|---|---|
| **qoder-cn** | ✅ `.qoder/agents/*.md` | ✅ `key_sub_agent_type`（实测 16 条） | 唯一完整可用 |
| Qoder 标准版 | ❌ UNSUPPORTED | ❌ | 与 package 口径一致 |
| **codex** | ❌ 无名字 | ⚠️ `is_subagent`（30 抽样中 8 个） | `cwd` 在 `payload.cwd`（不是顶层）；只能标"未命名子会话" |
| **claude-code** | ❌ UNSUPPORTED | ❌ | `subagents/` 只计数、不读入 |

---

## 5.1 qoder-cn 的数据源事实（照 package 的做法）

package 的 `adapters/qoder.py` 不用 JSONL 判断子 Agent，而是读 `local.db`：

```sql
SELECT session_id, project_uri, model FROM chat_session;
SELECT session_id, session_type, mode, parent_session_id FROM chat_session;
SELECT session_id, parent_session_id, parent_tool_call_id, extra FROM chat_record;
```

- `chat_session.project_uri` = 会话所属工作区（≈ `cwd`）；
- `chat_record.extra.key_sub_agent_type` = 子 Agent 名称；
- `parent_session_id` / `parent_tool_call_id` = 子会话溯源。

**因此本地实现也应按这个口径**：qoder-cn 的"会话清单 + 子 Agent 归属"来自 DB，
transcript（JSONL）只用于结构证据。两者用 `session_id` 关联。

---

## 5.2 计数口径（唯一）

全仓只用 **事件数（events）**：

| 来源 | 事件数取什么 |
|---|---|
| qoder-cn（有 DB） | `chat_message` 的条数 |
| qoder-cn（仅 transcript） | transcript 行数 |
| codex / claude | transcript 行数 |

`transcript_lines` 单独保存，只用于"结构证据"；**任何跨 Agent、跨工作区的对比都只看 `events`**。
字段名 `line_count` 已全部改为 `event_count`，避免"行"和"条"混用。

---

## 6. 隐私边界

- `ws_ref = sha256(真实路径)` 前缀；**对外的任何输出只出现 ws_ref，不出现绝对路径**；
- 真实路径只存在本机 `workspaces.json` 与 `unit.json`；
- 导出/发布时剔除 `cwd` 与绝对路径；
  **`sessionId` 不剔除**——它是 uuid，推不出路径，而组织侧复核与锚点解析都依赖它
  （见 [ASSET-SCHEMA.md §2 与 §2.1](ASSET-SCHEMA.md)）。本条此前写"剔除 sessionId"，与那份文档相反，已按锚点模型统一为保留；
- **可上传产物与本机索引层不含会话正文**（现状：digest 只存结构）。
  正文的唯一本机落点是 vault——已定稿、未实现，规则是"只在本机、可整目录删除、绝不出网"
  （[CONTEXT-VAULT.md](CONTEXT-VAULT.md) 的 C3/C7/C8）。在那一层落地前，本条按字面成立。

---

## 7. 对 S1 / S2 的映射

**发现页（S1）**

```
qoder-cn
 ├─ ● Default Agent        工作区：☑ ctx_parser  ☑ lab      [启用]
 ├─ ○ reviewer（子）        工作区：ctx_parser（固定）        SOURCE_VERIFIED
 └─ ○ doc-writer（子）      工作区：ctx_parser（固定）        DISCOVERED
```

- Default 的工作区**可多选**；子 Agent 的工作区**只读**（由配置位置决定）；
- 未安装 / 不支持的 harness 明确标注，不做假数据。

**关系图（S2）**

```
Unit（工作区）
 ├── Agent: Default（qoder）── Session × 27
 ├── Agent: reviewer（子）   ── Session × 3     ← 用 parent 关系连到 Default
 └── Agent: Default（codex） ── Session × 12
```

---

## 8. 待确认（确认后才动代码）

1. **`agent_id` 生成**：本仓自行生成 `agt-<hex>`（与 package 同格式、不同值），将来接入 ACPE 时用**外部键**做映射——接受吗？
2. **子 Agent 的 workspace 只读**：界面上不可改（由 `.qoder/agents/*.md` 所在位置决定）——接受吗？
3. **codex 的"未命名子会话"**：归 Default 并打标，还是直接排除？
4. **selection.json 的粒度**：只选 Default Agent（子 Agent 自动跟随）——接受吗？

---

## 9. 确认后的代码改动清单

| 模块 | 改动 |
|---|---|
| `lab/state.py`（新） | 全局 state 根、workspaces/agents/harnesses 读写 |
| `lab/agents.py`（新） | 子 Agent 发现、验证、`ws_ref` 计算 |
| `lab/digest.py` | 归属从「source」改为「agent_id」；读 `key_sub_agent_type` |
| `lab/graph.py` | 第二层改为 Agent；子 Agent 用 parent 连线 |
| `web/` | 发现页改为「harness → agent → workspace」树；selection 页 |
| `groma_shell/installer` | 安装时写 `workspaces.json` + `unit.json` |
