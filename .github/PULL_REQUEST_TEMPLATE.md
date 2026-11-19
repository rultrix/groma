# PR 检查单

**改了什么、为什么**

**测试（照 CONTRIBUTING 口径报数）**

- [ ] `Ran N / 通过 / skipped M`（runner × 解释器）：
- [ ] 新增用例已在未修版本上证红（如无新用例，写"无"）：

**红线核对**

- [ ] 不联网 / 不读凭据 / 只监听 127.0.0.1 未被破坏
- [ ] `web/templates/**` 零 JS（无 `<script>` / `on*=` / `javascript:`）
- [ ] 逐路径 `git add`，未卷入他人未提交文件（`git status --short` 核过）

**文档**

- [ ] 行为变化已同步进 `README.md`「变更记录」表 + `VERSION`（产品级变更）
