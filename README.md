# Groma — 本地优先的 AI 编码 Agent 观测与回执运行时

> **状态：实验性项目（spike），API 与数据格式仍会变。**
> Groma（0.53 之前的名字是 wb）验证一个假设：个人用户需要一个装在本地的运行时，来承载
> 「我的 AI 编码 Agent 都做了什么」的可复核记录——不依赖任何云服务。

## English

**Groma** (formerly `wb`) is a local-first runtime for observing and notarizing what your
AI coding agents actually did: byte-level receipts over your files, continuous
cross-harness session collection, and a zero-JavaScript local web UI.

- **No network access. No credentials. Listens on `127.0.0.1` only.**
  The sole outbound call is an update check — triggered **only by an explicit user click**,
  a read-only GET against the GitHub Releases of `rultrix/groma`
  (overridable via `GROMA_RELEASE_REPO`); no background or silent checks.
- Core is pure standard library (Python 3.11+); the web UI needs only Flask.
- No external repositories or services are required at runtime: Groma never clones, fetches, or calls an upstream code repository.
- One-command acceptance check: `bash examples/quickstart.sh`
- *Groma* is named after the Roman surveying instrument that established straight lines and
  right angles with plumb lines — the same idea: turning "what actually happened" into
  something you can check.
- Chinese name: **硅矩-鲁班尺** — "the craftsman's rule of the silicon age". A display name
  only: the app, tray, installer and `--version` all still say **Groma**.
- Docs are currently mostly Chinese; an English handbook may follow.

## 为什么是 Groma

用 AI 编码 Agent 干活的人，很快会遇到三个问题：

- **说不清**：几个 Agent（甚至不同家的 Harness）在同一个项目里各改了什么？
- **对不上**：上个月那版能跑的代码，和现在这版到底差在哪？
- **不敢给别人看**：想把工作过程给协作者或工具链看，又不想把代码传上云。

Groma 的答案是一个**装在本机、只读、可复核**的运行时：它给你字节级的回执、
跨 Harness 的会话观测、和一个只监听 `127.0.0.1` 的本地网页。

## 核心保证（信任边界）

这些是设计红线，不是最佳努力：

- **不主动联网**：本地观测、回执与采集核心不访问网络、不上传任何数据。检查更新 / 下载更新
  只在**用户明确点击**后发生，且**只读 GET `rultrix/groma` 的 GitHub Release**
  （坐标可用环境变量 `GROMA_RELEASE_REPO` 覆盖，仅允许 GitHub 域跳转），
  不上传本机数据、不读凭据、不发设备标识，也没有后台或静默检查；
- **不读凭据**：不读取、不存储任何密钥或密码；
- **只监听 `127.0.0.1`**：Web 界面不对外暴露；
- **只读元数据**：采集的是会话记录的结构信息（时间、路径、规模），不搬运文件正文；
- **声明式读取**：单元之外的目录，不显式声明就当不存在——工具不会自己去翻你的盘；
- **界面零 JavaScript**：服务端渲染，`curl` 就能断言页面内容；
- **零外部代码仓依赖**：core 只用 Python 标准库，Web 层只可选 Flask；Groma 不克隆、不拉取、
  不访问任何外部代码仓或服务，示例与测试同样不调用网络。

## 三层能力

| 层 | 问题 | 形态 |
|---|---|---|
| **L1 回执** | 能不能对一组文件给出可复核的字节级记录？ | CLI：`init` → `attach` → `receipt` → `verify` |
| **L2 采集** | 能不能连续、无需人工介入地采集？ | 声明采集根 + 定时/事件驱动扫描 + 结构摘要 |
| **L3 界面** | 非技术用户能不能自己上手？ | 本地网页（六个页面）→ 桌面壳（窗口+托盘）→ 安装包 |

## 快速开始

要求 Python **3.11+**（实测 3.11.9）。core 零第三方依赖。

**一条命令验收**（在仓库根目录，Git Bash）：

```bash
bash examples/quickstart.sh
```

它在临时目录里跑完整套流程（建单元 → 登记 Agent → 出回执 → 校验 → 改一字节 →
删文件 → 空单元），逐条打印退出码，**全对才退出 0**。这个脚本本身被测试覆盖，
跑不通测试就红——所以它不会像手抄文档那样悄悄过期。

**手动五步**（理解「到底发生了什么」）：

```bash
# 0) 先往单元里放你要记录的东西（init 只建根目录和 .groma/，第 0 步不能跳）
mkdir -p "C:/my-space/sub"
printf 'hello\n' > "C:/my-space/note.txt"
printf 'alpha\n' > "C:/my-space/sub/inner.txt"

# 1) 建一个本地单元（根目录不存在时，init 会自己创建）
python groma.py --root "C:/my-space" init --name my-space

# 2) 登记一个 Agent（只记录，不调用 Harness）
python groma.py --root "C:/my-space" attach qoder --harness qoder

# 3) 给单元里的文件做字节级回执
python groma.py --root "C:/my-space" receipt --note "first try"

# 4) 重算并比对（把上一步输出里 "receipt" 的值原样粘进来，不要自己拼路径）
python groma.py --root "C:/my-space" verify "<上一步输出的 receipt 值>"
```

`groma.py` 启动器可在任意目录执行；也可以在仓库根用 `python -m lab --root …`。
退出码是脚本判定的唯一依据，完整表见 `groma.py --help` 与源内文档。

## 本地网页

```bash
python -m venv .venv
.venv/Scripts/pip install "flask>=3.0"     # macOS/Linux: .venv/bin/pip

# 启动（默认只监听 127.0.0.1；启动时自动跑一次补扫）
python groma.py --root "C:/my-project" web --port 8787
```

概览 / 发现（本机已知 Harness）/ 来源 / 活动 / 资产 / 工作区六个页面；
关系图（`/graph`）由服务端直接生成 SVG，同样零 JavaScript。

## 桌面壳与安装包（Windows）

```bash
.venv/Scripts/pip install pywebview pystray pillow pyinstaller
python groma.py --root "C:/my-project" shell --port 8787   # 窗口 + 托盘，一个进程
```

构建安装包（两条 spec 都在仓里，可复现）：

```bash
.venv/Scripts/python.exe -m PyInstaller --noconfirm --clean groma.spec          # -> dist/groma/（onedir）
cp VERSION dist/groma/VERSION
.venv/Scripts/python.exe -m PyInstaller --noconfirm --clean install-groma.spec  # -> dist/install-groma.exe
```

安装到 `%LOCALAPPDATA%\Programs\Groma`，免管理员；卸载不删工作区里的 `.groma/`（那是用户数据）。
macOS 的窗口与托盘未测（`NOT_TESTED`）。exe 未签名，首次运行会有 SmartScreen 提示。

## 从 wb 迁移

0.53 是新名字的第一个版本。旧机器上的状态目录、工作区元数据、身份卡、注入面与计划任务
可以一次搬完；默认只打印计划，加 `--yes` 才写盘：

```bash
python groma.py migrate          # 预览：会移动/改名/重装哪些东西
python groma.py migrate --yes    # 执行；项目文件与 Harness 数据不动
```

旧名字在**这一个版本**里仍可读：`wb.py`、`.wb/`、`wb-unit.json`、`WB_*` 环境变量、
`WBLOCAL-*` 计划任务。迁移后再跑一次预览，应只剩 `current`。

## 跑测试

```bash
python -m unittest discover -s tests
```

core 是纯标准库；只有 Web 层（`tests/test_web.py`）需要 Flask，没装时自动 skip。
报数口径：`Ran N / 通过 / skipped M` 三段——`skip` 不算过。

## 文档

- `docs/DATA-MODEL.md` / `docs/ASSET-SCHEMA.md` —— 数据模型与资产 schema（外部集成看这两份）
- `CONTRIBUTING.md` / `SECURITY.md` —— 贡献指南与安全策略
- 分层架构总览正在整理成公开版，暂不随仓发布

## 明确不做的事

- 不做任何 Harness 的真实采集执行（`attach` 只是登记）；
- 不复制或修改上游工具的代码；
- 不做界面之外的特权动作；
- 不定义授权模型。

## 关于这个名字

Groma 是古罗马的测量仪器：从垂直支架顶部挂下可旋转的十字横臂，两端垂铅锤线，
靠视线对齐铅锤来确定直线与直角——用于划地、修路、铺水道。

选这个名字，是因为它和这个工具做的是同一件事：**把看不见的东西变成可检验的基准**。
铅锤线确立的是地面的直角，Groma 确立的是"哪些文件、在什么时候、被谁改成了什么样"。

**中文名：硅矩-鲁班尺**——「矩」是画直角和方形的工具（"不以规矩，不能成方圆"），
和 groma 定直线直角是同一件事，只是换了语系；「鲁班尺」取工匠祖师那把量具的意思，
说的是**工匠的基准尺**，与行业里那种算吉凶的同名尺无关。合起来就是：**硅基时代的鲁班尺**。

> 应用窗口、托盘、安装包与 `--version` 里显示的名字**仍然是 Groma**；
> 中文名只用于介绍与文档，不改任何可见界面文案。

## 许可证

[MIT](LICENSE) © RulTrix
