# 贡献指南

Groma 是一个本地优先（local-first）的 Agent 观测与回执运行时：不联网、不读凭据、
Web 界面只监听 `127.0.0.1`。动手前请先读根 `README.md` 的「明确不做的事（L1）」——
那几条红线（不做真实采集、不复制 RSCL 代码、不做界面之外的特权）同样约束贡献。

## 环境

- Python **3.11+**（开发实测 3.11.9）
- core **零第三方依赖**；只有 Web 层（`tests/test_web.py`）需要 Flask：

```bash
python -m venv .venv
.venv/Scripts/pip install "flask>=3.0"   # macOS/Linux: .venv/bin/pip
```

## 跑测试

```bash
python -m unittest discover -s tests
```

两条本项目特有的口径，提 PR 时请照办：

1. **报数用三段式**：`Ran N / 通过 / skipped M`（`Ran N` 已含 skipped，
   不要写"X passed + Y skipped"——那是双算）。**`skip` 不算过**：条件跳过的用例
   要注明在哪个平台才真跑。
2. **新测试先证红**：新用例要打在未修版本上证明它会失败（`git archive <旧提交>`
   抽临时树跑），做不到就别声称这条测试守住了什么。整文件红只证明"函数没实现"，
   把函数留着、只删调用点再跑一遍，才证明测试钉的是接线。

## 代码约定

- Web 模板**零 JavaScript** 是产品红线：`<script>` / `on*=` / `javascript:` 一律不进
  `web/templates/**`。
- 共享文件并发编辑时**逐路径 `git add <path>`**，禁止 `git add -A` / `git add .`；
  提交前 `git status --short` 核一遍没有卷进别人的在制品。
- 文档里写"已实现"之前先问一句：代码里有 ≠ 跑出来对 ≠ 已进构建 ≠ 产物已重写。

## 提交

- 不 force-push；历史重写只允许打在未推送的提交上。
- 提交信息格式：`type(scope): 中文一句话`（如 `fix(test): ...`、`feat(web): ...`）。

## 语言

仓内文档以中文为主；issue / PR 用中文或英文均可。
