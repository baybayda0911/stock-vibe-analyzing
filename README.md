# stock-vibe-analyzing

面向股票研究的中文 Agent Skills 集合。不同分析任务使用各自独立、可安装的 skill，后续逐步加入技术面等研究工作流。

**当前仓库版本：v0.1.0。** [版本发布页](https://github.com/baybayda0911/stock-vibe-analyzing/releases/tag/v0.1.0)。

仓库地址：[baybayda0911/stock-vibe-analyzing](https://github.com/baybayda0911/stock-vibe-analyzing)。源码与独立 skill 分发包均采用明确的发布白名单，研究报告和金融数据不随包分发。

## Skill 清单

| Skill | 中文名 | 版本 | 状态与用途 |
|---|---|---|---|
| [stock-fundamental-analysis](skills/stock-fundamental-analysis/SKILL.md) | 股票基本面分析 | 0.1.0 | 已编写；长期公司研究、财务质量、护城河、增长、风险及估值 |
| 技术面分析 | 待后续定义 | — | 规划中，尚未创建 |

目前只有首个基本面 skill。每个 skill 的版本记录在其 `SKILL.md` 的 `metadata.version`；仓库整体版本以 [VERSION](VERSION) 为准，两者分别维护。

## 首个 skill 的能力

`stock-fundamental-analysis`（股票基本面分析）基于原提示词和完整公司研究框架，提供：

- 自动选择的中文基本面研究入口，覆盖“请分析XX公司基本面”“请分析XX股票基本面”等请求。
- 九章 Markdown 报告流程，包含五年及近期财务、三种估值情景、反方审查、证伪条件与季度跟踪卡。
- 一手资料优先、引用追溯、财务口径检查与数据不足时的降级规则。

以上是已编写的工作流指令，实际研究由宿主 agent 执行。v0.1.0 没有独立行情服务、交易程序或自动监控器。

## 环境与依赖

需要能读取本地文件并支持 Agent Skills 的宿主。已在 Codex CLI 与 Trae 3.6.4 完成全局安装及自然语言调用测试；`agents/openai.yaml` 提供 Codex 元数据，Trae 使用通用 SKILL 入口。其他宿主尚未实测。

首个 skill 只有 Markdown 与 YAML，不需要 Python、Node、包安装、API Key 或环境变量。研究最新公司资料时，需要宿主已有的联网检索能力；可选的只读金融数据工具由用户自行配置。无法联网时可以分析用户提供的文件，但必须标明资料截止日与缺口。以后新增 skill 的依赖应按各自实际入口说明。

## 在 Codex 中安装基本面 skill

在包含本 README 的仓库根目录执行以下 PowerShell 命令，只安装选定的 skill：

```powershell
$skillHome = Join-Path $HOME '.agents/skills'
$skillTarget = Join-Path $skillHome 'stock-fundamental-analysis'
if (Test-Path -LiteralPath $skillTarget) { throw '目标已存在，请先核对安装版本并备份，不要直接覆盖。' }
New-Item -ItemType Directory -Path $skillHome -Force -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath 'skills/stock-fundamental-analysis' -Destination $skillTarget -Recurse -ErrorAction Stop
```

目标目录应包含 `SKILL.md`、`LICENSE`、`agents/openai.yaml`、`references/prompt.md` 和 `references/公司研究框架.md` 五个文件。不需要复制整个集合仓库或其他 skill。项目级安装可改用对应项目的 `.agents/skills`，避免同名安装多份。独立分发 skill 时保留其中的许可证文本。

安装路径、自动选择与显式调用的依据见 [OpenAI 官方 skill 文档](https://learn.chatgpt.com/docs/build-skills)（核对日期：2026-10-27）。Codex 按 skill 描述选择是否调用，`allow_implicit_invocation: true` 允许自动选择；自然语言触发取决于宿主判断，并非固定关键词拦截器。新安装通常会自动发现；若未出现，重启 Codex 后检查 skill 列表。

2026-10-27 已按用户授权完成 Codex 与 Trae 用户级全局安装，并使用原句“请分析中际旭创基本面”验证自动调用；两端均实际读取全局 skill 并生成分析。安装文件与仓库一致，具体证据及研究质量边界见 [验证记录](docs/validation.md)。

## 在 Trae 中安装基本面 skill

在仓库根目录执行以下 PowerShell 命令，复制到 Trae 的全局 skill 目录：

```powershell
$skillHome = Join-Path $HOME '.trae/skills'
$skillTarget = Join-Path $skillHome 'stock-fundamental-analysis'
if (Test-Path -LiteralPath $skillTarget) { throw '目标已存在，请先核对安装版本并备份，不要直接覆盖。' }
New-Item -ItemType Directory -Path $skillHome -Force -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath 'skills/stock-fundamental-analysis' -Destination $skillTarget -Recurse -ErrorAction Stop
```

实际验收使用 Trae 当前已配置的 GPT-6.1-Sol Max，自然语言请求未点选 skill 或使用斜杠命令。该结论覆盖本次版本和用例，不表示其他模型及所有请求均已验证。

## 使用基本面 skill

安装并确认宿主已发现后，在对话中输入：

```text
请分析贵州茅台（600519.SH）公司基本面。
请分析苹果（NASDAQ:AAPL）股票基本面。
```

也可以明确指定：

```text
请使用 $stock-fundamental-analysis 分析腾讯控股（0700.HK）基本面。
```

预期先核实公司及目标证券，读取研究依据，检索原始披露，再生成中文 Markdown 报告：核心结论、商业模式、行业、竞争、客户、财务与管理层、增长及风险、估值、季度跟踪。没有用户提供的组合信息时，不机械给出仓位比例。

同名公司或多地上市造成估值歧义时，agent 会先确认证券；缺财务资料、现价或联网能力时，应明确给出初步研究和无法判断项。报告默认在对话内交付；要保存文件时指定输出位置。跟踪卡不自动创建提醒或持续监控。

如自动选择没有发生，先检查发现目录、是否禁用、名称冲突和 skill 描述是否被宿主截断，再使用显式调用确认入口可读。纯股价查询、技术指标或新闻摘要不属于基本面 skill 的默认范围；后续技术面 skill 应另行设计触发描述。

## 目录与文档

```text
stock-vibe-analyzing/
├── README.md                         仓库导航、Skill 清单及安装使用
├── AGENTS.md                         仓库维护入口
├── VERSION                           仓库整体版本
├── CHANGELOG.md                      按 Skill 归属记录仓库版本变化
├── LICENSE                           MIT 标准许可证
├── .gitignore                        本机配置和研究产物的忽略规则
├── skills/
│   └── stock-fundamental-analysis/
│       ├── SKILL.md                  基本面入口及自身版本
│       ├── LICENSE                   独立分发所需的 MIT 声明
│       ├── agents/openai.yaml        展示信息、示例调用与自动选择策略
│       └── references/
│           ├── prompt.md             原 TXT 转换的研究提示词
│           └── 公司研究框架.md         完整专题及原 V3.0 研究规范
├── release-files.txt                 逐文件发布白名单
├── scripts/build_release.py          标准库发布归档工具
├── tests/test_release.py             排除与失败保护测试
└── docs/
    ├── maintenance.md                文档归属、多 Skill 扩展及版本规则
    ├── validation.md                 验证方法、用例与实际结果
    └── releasing.md                  发布白名单、归档及远程核验
```

两份研究依据已迁入首个 skill 的 `references/`，原始框架内容不变，不保留第二份来源。以后新增 skill 放入 `skills/<skill-name>/`，带齐自身需要的资料，详见 [维护与扩展规则](docs/maintenance.md)。技术面 skill 尚未创建，不预建空目录。

用户生成的报告和下载资料可分别放在被忽略的 `reports/`、`data/`，当前不创建空目录。`.gitignore` 只影响未跟踪文件，不能清理已提交的内容。

## 验证与限制

2026-10-27 在 Windows／PowerShell 环境完成原资料保留核验、skill 官方结构校验、YAML／相对链接／版本检查及单独安装包的隔离复制验证，详见 [验证记录](docs/validation.md)。

随后完成两端全局安装与中际旭创自然语言自动调用，并生成研究结果。关键现金流计算抽查通过；发现 Trae 所引一项毛利率与财报复算不符，交付版已附更正，详见验证记录。实际报告仍有订单条款、分产品盈利、资本回报等缺口，不能把触发成功等同于投资结论已经完整核实。2026-10-28 进一步执行离线资料、历史时点、证券歧义、纯价格请求与毛利率冲突处理的行为验收；具体宿主和证据边界见验证记录。跨版本兼容及全部预测准确性尚未验证。

发布附件由 [发布脚本](scripts/build_release.py) 按 [白名单](release-files.txt) 生成，集合源码包仅含 17 个核验文件，基本面 skill 包仅含五个文件。维护测试和打包使用 Python 3.10+ 标准库，不是 skill 的运行依赖。步骤见 [发布核验](docs/releasing.md)。季度业绩与市场定价的新板块仍是仓库外评审样本，未纳入 v0.1.0。

## 版本与更新

仓库版本以 [VERSION](VERSION) 为准；各 skill 在自身 frontmatter 中维护版本。变更统一记录在 [CHANGELOG](CHANGELOG.md)，升级及发布顺序见 [维护文档](docs/maintenance.md)。仓库与基本面 skill 均为 `0.1.0`，标签 `v0.1.0` 标记整个仓库快照。

新增其他 skill 可以提升仓库版本，同时保留未变化 skill 的版本。用户只更新实际需要的安装副本；替换前先核对并备份旧副本，保留用户自定义资料。

## 许可证与资料来源

首个 skill 的依据来自维护者提供的 [prompt.md](skills/stock-fundamental-analysis/references/prompt.md)（原 `prompt.txt`）与 [公司研究框架.md](skills/stock-fundamental-analysis/references/公司研究框架.md)。其中 V3.0 是原始提示词版本，与项目及 skill 版本独立。

本项目采用 [MIT 许可证](LICENSE)，由维护者于 2026-10-27 确认。基本面 skill 内保留同一份许可证，便于单独安装和分发；分发时保留版权及许可声明。第三方年报、研究或数据仍适用其各自使用条件，不随 skill 重新授权。
