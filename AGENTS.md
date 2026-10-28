# 仓库维护入口

先阅读 [README.md](README.md)、[维护与版本规则](docs/maintenance.md) 和 [验证记录](docs/validation.md)，再修改相关文件。

- `stock-vibe-analyzing` 是股票研究的多 Skill 集合仓库，每个独立 skill 放在 `skills/<skill-name>/`；当前只有 `stock-fundamental-analysis`（股票基本面分析）。
- 仓库版本以 `VERSION` 为准，各 skill 版本以自身 `SKILL.md` 中的 `metadata.version` 为准；变更统一记录到 CHANGELOG。
- 先读取目标 skill 的入口及相关资料，再编辑。各 skill 单独安装后必须自包含，不能依赖仓库根目录或相邻 skill 的文件。
- 基本面研究依据唯一维护于 `skills/stock-fundamental-analysis/references/`；不要在根目录或其他目录另存维护副本。
- 新增 skill 时更新 README 清单、CHANGELOG 和验证用例；触发描述区分基本面、技术面及其他场景，避免无意扩大调用范围。
- 不将文档基线写成已安装、已实测或已发布。保持中文 UTF-8 文档，不公开个人持仓、凭据、真实用户目录或本机代理。
- 本地编辑不自动授权安装全局 skill、初始化 Git、提交、推送、创建标签或发布。
- 若存在本机资料库管理手册，继续遵从它；连接 GitHub 前核对并显式使用当地要求的代理。只做文件安全检查时完全不运行 Git，报告后停止。
- 项目采用 MIT；独立 skill 分发包保留相同许可证，新增第三方资料时核对来源及其原有授权，不自行更改许可范围。
