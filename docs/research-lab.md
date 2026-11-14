# 研究室的安装与本地档案

研究室`stock-research-lab` 0.3.0是本地开发版，维护既有归档协议、可选明示附件及连续研究、交易复盘工作流。程序保存记录、验证关系和日期资格；判断兑现、原因与复盘文字由宿主结合真实证据生成。基本面与技术面形成专业研究；查找文件、个股首页／时间线、版本对比、资料包和完整快照恢复由[档案室](research-archive.md)提供。没有后台任务、券商连接或自动绩效计算。

## 独立安装

只复制`skills/stock-research-lab`到宿主支持的Skill目录。应有SKILL.md、LICENSE、agents/openai.yaml、references/研究室工作流.md、scripts/research_lab.py五个文件。运行需要Python 3.10+标准库；基本面可独立安装，配置研究室后消费其档案能力。

档案室独立包有七资源，复用此研究室工具；宿主须显式提供已核验`research_lab.py`的绝对位置并传`--lab-tool`，不假设安装包相邻或扫描全盘。研究室包自身不依赖档案室文件，入口分工与命令见[档案室说明](research-archive.md)。

安装、文件发现、自然语言路由和实际研究质量是不同验收项。此仓库的新文件不会自动改写既有全局安装；按维护文档核对和备份后再更新相应宿主。

图形[工作台S3](platform.md)沿用同一私有根，展示跨历史关注项、原判据、证据、窗口和回顾；追加人工回顾需理由及当前修订令牌。context返回原冻结original_items和有效item_states，并用同包共享角色区分正式研究、补充资料、未知模式。角色不替代逐项日期资格。名单只写platform/watchlist.json，快照/2可保留；工作区不作为正式源。专业执行留S4。

## 初始化私有根

在仓库根使用以下命令，将`$archiveRoot`设为用户已确认、Git仓库外的绝对目录。不要将报告保存在源码中：

```powershell
$archiveRoot = Read-Host '已确认的私有档案绝对目录'
python -X utf8 -B skills/stock-research-lab/scripts/research_lab.py init $archiveRoot
```

初始化建立README、config.local.json、catalog.json、securities和_staging。实际证券、研究日期和计划目录在收到记录时建立，不以空目录表示研究已经完成。重复初始化或已有记录不覆盖。

根可以由用户输入、`STOCK_RESEARCH_LAB_ROOT`环境变量，或当前项目被忽略的`.research-lab.local.json`中的`archive_root`指定；这些本机路径不进入发布白名单。不要为临时任务修改系统全局配置。

## 使用入口

对话示例：

```text
请回顾这只股票上次基本面研究中的关注项，结合本次资料更新并归档。
请使用 $stock-research-lab 回顾指定股票的旧判断和未结事项，记录证据及原因。
请按我提供的成交反馈，复盘指定计划版本，不假设未提供的费用和成交。
请导入我指定的旧报告，保留原日期与无法核实的部分。
```

运行命令和JSON字段见[研究室工作流](../skills/stock-research-lab/references/研究室工作流.md)。在Skill独立目录内也可使用自己的scripts与references，不依赖本仓库其他文件。

私有目录结构：securities／证券ID／fundamental或technical／研究日期／结果ID；reviews按回顾日期；plans按计划ID及版本；executions按成交月份及导入ID；imports保存指定旧档原件和适配记录。未知旧研究日期进入undated。catalog可重建，不能代替原报告和原始记录。

研究室与档案室使用同一私有根、一套源记录和一个派生catalog，不维护两套最新结论。档案室的`view/compare/export`在根内写`_workspace/views`的新目录，手动`backup`快照写`_workspace/backups`，`restore`演练写`_workspace/restore-tests`的新目录；工作区排除在正式记录扫描和快照源之外，原securities、稳定ID及旧文件不搬迁。`verify`校验快照，恢复重建catalog但不覆盖正式根。快照不加密，同盘副本不构成硬盘故障容灾。

## 日期和结果边界

首页同时展示报告输出日、研究／数据截止与逐项适用状态。重新导出不刷新旧证据；过期内容不可直接用于当前决策，但保留作历史对账。新财报或行情由宿主取得后复核，工具不会自动发现市场事件。

当前`list`与`context`提供相同的结构化读档结果，包括原记录、排除原因、逐项资格、跨历史未结项、计划和成交记录。某项可引用不代表整份报告都适用。新回顾只追加，不用新目标修改旧判断；无方向／期限的观察项不算预测命中。

同一回顾混合财务、行情、暂停分钟等不同数据覆盖时，默认`data_asof=null`并逐项说明实际覆盖，或按可证相同截止拆分回顾；不能用最新行情日期批量刷新旧项。具体填写规则见[持续回顾协议](../skills/stock-research-lab/references/研究室工作流.md#持续回顾)。

计划与成交关联具体版本，真实与模拟分开；缺反馈是执行未知。程序不计算P&L，必要的盈亏由宿主在数量、成本、费用和币种可靠时复核。真实研究、数据、成交、原件、配置和运行日志留在私有根或用户指定的仓库外位置。

## 可选附件和严格复盘

`archive`／`import`可选`attachments`数组，每项仅有`source_file`、`relative_path`、`media_type`。源是非Git且不经链接的私有绝对普通文件；只接受JSON／PNG／HTML及对应媒体类型，报告相对链接须与归档路径一致。目标路径、类型、重复／重叠、控制文件名均受检查；完整协议及示例唯一维护在[研究室工作流](../skills/stock-research-lab/references/研究室工作流.md)。缺省或空清单兼容旧请求，不能据此声称所有图与JSON已自动归档。

报告、明示附件、items与manifest先暂存再整体提交。manifest保留附件相对位置、实际大小和SHA256；读档／重建索引时复验。删除源快照后，只有已复制并列明的文件可由归档恢复。复制失败不提交部分成果，索引失败保留已保存内容并单独报告；缺失或篡改不能靠重建索引消除。`review`仍只存回顾报告，完整新技术快照应作为新成果归档。

严格复盘分开条件／分析结果、计划采纳、执行事实与收益资格，问题分类为数据／计算、分析／条件、计划、执行、外部／未知，可多因并存。允许“已触发但执行受限”“用户明确未做”“无反馈执行未知”“部分执行”；无事前方向、期限或判据不计预测命中，未交易不造trade或0收益。

这些说明写在回顾正文及合法`reason`中；`review.updates`仍是现有白名单，不接受任意冲突／反馈字段。条件、计划版本、实际成交、反馈收到与收益估值时点分开；更正追加且不双计，不能从图上触价反推成交或从盈亏反推唯一责任。

## 验证

```powershell
python -X utf8 -B -m unittest discover -s tests -v
```

测试用临时根与虚构小样本，不写正式用户档案。实际覆盖范围、宿主状态及未做事项以[验证记录](validation.md)为准。当前源码不代表全局安装已经升级、自然语言触发已经通过或已经发布；源码与独立分发包不含真实档案、查阅页面、快照及恢复副本，发布按[分发核验](releasing.md)执行。
