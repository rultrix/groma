# 上下文资产草稿（S3 · DRAFT v0.3）

> **文档版本 v0.3** ｜ 修订：2026-11-05 ｜ 对应 Groma **v0.39.0** ｜ 类型：现状 + 目标形状混合
> （§2.1 的 `profile` 与 `anchors` 是**目标形状，代码尚未落地**；其余描述现状。产品级变更只进 `README.md` 的变更记录表。
> v0.3 只补了 `session_id` 的口径（现在与 digest/vault 同一把键）与 `asset_id` 的派生边界。）

> 范围：**只生成草稿，不发布**。按 RSCL 的四件套组织，发布到组织时只差"复制 + PR"。
> 原文还写着"不注入"——**那句已过时**：S4 起可 `asset export` 出 `CONTEXT.md`，v0.38 起由 Groma 生成并拥有
> Qoder 注入 hook（见 `README.md` 的 v0.29 / v0.38 条目与 `lab/hooks.py`）。
> 依据：RSCL `Context-Layer-Protocol`（白名单 / 禁发项 / 证据分层），以及 mvp2pmf、evaluator 仓的
> `docs/rscl/*` 实例。

---

## 1. 资产目录

```
<workspace>/.groma/assets/<asset_id>/
  ├── asset.json       机器可读：来源、Agent、统计、状态
  ├── README.md        人读：这份资产是什么、依据哪些会话
  ├── VALIDATION.md    证据分层：生成了什么、核对过什么、什么没验证
  └── manifest.json    文件白名单 + 每文件 sha256（不含自身）
```

`asset_id = "asset-" + sha256(workspace_ref + 排序后的会话键 + title)[:12]`。
**同样的选择 → 同样的 id**，重复生成是覆盖而不是新增。

---

## 2. `asset.json`

```json
{
  "schema": 1,
  "kind": "GROMA_LOCAL_CONTEXT_ASSET",
  "asset_id": "asset-1a2b3c4d5e6f",
  "title": "Qoder 采集修复过程",
  "status": "draft",
  "generated_at": "2026-10-30T01:00:00Z",
  "workspace_ref": "ws-a286beaee32b2e54",
  "unit_id": "unit_26a70f876481c77f",
  "org_path": "github.com/example/upstream-harness",
  "workspace_name": "ctx-parser",
  "digest": "digest-20261029T140753Z.json",
  "digest_sha256": "…",
  "agents": [
    {"agent_id": "agt-…", "name": "qoder-ctx-parser", "kind": "sub", "sessions": 2, "events": 180}
  ],
  "sessions": [
    {
      "source": "qoder",
      "path": "projects/0f06e741-….jsonl",
      "session_id": "0f06e741-…",
      "agent_id": "agt-…",
      "agent_name": "qoder-ctx-parser",
      "agent_kind": "sub",
      "events": 94,
      "transcript": true,
      "first_ts": "2026-10-20T…",
      "last_ts": "2026-10-20T…"
    }
  ],
  "totals": {"sessions": 2, "events": 274},
  "content_stored": false,
  "content_mode": null,
  "profile": "index-only",
  "notes": ""
}
```

**内容型资产（S5 试跑）**：`content_stored: true` 时，`README.md` 承载语义摘要，`export` 直接把 README 作为 `CONTEXT.md` 的正文，不再只导出索引。额外字段：

```json
{
  "content_stored": true,
  "content_mode": "semantic-summary",
  "content_status": "prototype",
  "content_sections": ["background", "goal", "decisions", "constraints", "open_issues", "reusable"],
  "source_verified_at": "2026-10-30"
}
```

内容型资产仍然**不含 cwd / 绝对路径**；事实与归纳的区分写在 `VALIDATION.md`。
当前内容抽取是人工试跑，尚未固化为流水线（见 §5）。

### 2.1 供给档位 `profile`（本节的默认值）

> 字段形状已定，**代码尚未落地**：`build_asset()` 现在硬写 `content_stored: False`，也没有 `profile`
> （[lab/asset.py:181](../lab/asset.py)）。下面描述的是目标形状。

资产默认 `"profile": "index-only"`——只有结构与出处，**不含任何对话内容**。四档递进：

| 档位 | 资产里多出什么 | 默认 |
|---|---|---|
| `index-only` | 无 | ✅ |
| `claim-anchored` | **被结论引用到的那一小段原文** + 锚点 | 需显式升档 |
| `redacted-transcript` | 去凭证、去绝对路径后的区间原文 | 需显式升档 |
| `full-transcript` | 区间原文全量 | 默认不可达，逐次授权 |

升档是**逐条资产、逐次**的动作，不是全局开关；正文本身不在资产里"存一份库"，它来自本机 vault
（[docs/CONTEXT-VAULT.md](CONTEXT-VAULT.md)），资产带的是锚点：

```json
"anchors": [
  {
    "session_key": "qoder-cn:fdf7c2c9-…",
    "granularity": "turn",
    "from_ts": "2026-10-30T03:11:02Z",
    "to_ts": "2026-10-30T03:14:47Z",
    "stored_sha256": "…",
    "profile": "claim-anchored",
    "extracted_at": "2026-10-30",
    "extractor": "agt-…"
  }
]
```

于是禁止项分两类，不再是一刀切：

- **永远禁止**：绝对路径、`cwd`、凭据。（`path` 是相对来源根的路径；`session_id` 保留用于溯源。）
- **`session_id` 是会话自身的 id，不是文件名**（v0.3 起，`lab/asset.session_id_of`）：优先取 digest 量好的
  `session_key` 尾段，取不到才用 `digest.normalise_session_id()` 从文件名推。旧写法 `Path(path).stem` 对
  qoder 恰好等于 uuid，对 codex 却是 `rollout-2026-08-10T17-34-36-<uuid>`——把采集时刻当身份，
  家仓最新 digest 实测 124 行里 49 行与 digest 分叉。**对齐口径**：`session_key == "<source>:<session_id>"`，
  vault 的 `index.json`（未实现）将来按这个等式与资产行 join。
- **`asset_id` 只由 `(ws_ref, [(source, path)…], title)` 派生**，不含 `session_id`：改 session id 的拼写
  不许重铸已发布资产的 id（有测试钉这条）。
- **按档位禁止**：对话正文——`index-only` 下不得出现；`claim-anchored` 及以上允许，但**每一段都必须带
  上面那条锚点**，没有锚点的引文视为无效（组织侧无法复核，等于没给证据）。

这条区分很重要：组织侧聚合发生在 context layer（文件形态、可 PR 可复核），**那里没有中心正文库**，
所以"结论 + 有锚点的引文"就是复核所能依赖的全部；锚点缺失的资产不该被合并进组织视图。

---

## 3. `VALIDATION.md` 的固定结构

```markdown
# 验证状态

- 生成：draft（由 Groma 自动生成）
- 人工复核：未进行
- 内容留存：无（只保存结构与出处）

## 已具备
- 来源会话可在本地 digest 中定位（给出 digest 文件名与 sha256）
- 每个文件有 sha256（见 manifest.json）

## 未验证
- 结论是否正确：未评估
- 是否被 Agent 使用：未证明
- 是否产生效果：未评估
```

---

## 4. 与 RSCL 的对应关系

| 本地 | RSCL |
|---|---|
| `manifest.json` | `asset-manifest.json`（白名单 + sha256） |
| `VALIDATION.md` | 同名证据分层 |
| `asset.json` 的 `sessions` | `dependencies.json` 的来源（本地会话，非跨仓引用） |
| `status: draft` | 待人工复核 / 待发布 |

发布到组织时：把这四个文件放进目标仓的 `docs/…`，补一份 `github-retrieval.json`，开 PR。
**本地模型不需要改。**

---

## 5. 本期不做（S3 范围）

- 不发布到任何远端；
- 不注入 Harness；
- 不做人工编辑（草稿是生成物）。

S5 试跑已额外支持**内容型资产**（语义摘要 + README 作为导出正文），但抽取仍是人工流程；流水线化不在 S3 范围内。
