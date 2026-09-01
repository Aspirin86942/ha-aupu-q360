# 奥普 Q360T5-Pro Home Assistant 自定义集成

本项目用于将用户自有的奥普 Q360T5-Pro 浴霸照明接入 Home Assistant。

当前状态：协议抓包、`App-Authorization` 静态恢复和离线验证已经完成；Home Assistant 集成尚未开始实现。正式实现以前，以设计规格为准：

- [集成设计规格](docs/superpowers/specs/2026-09-02-aupu-q360-ha-integration-design.md)
- [脱敏协议分析](docs/research/q360t5-ha-analysis-redacted.md)

安全边界：

- Git 中不保存 JWT、手机号、设备标识、签名常量、Cookie、验证码或原始抓包。
- `.private/` 和 `local-evidence/` 仅供本机使用，已由 `.gitignore` 排除。
- 开发测试默认完全离线；未经用户再次明确授权，不重放控制请求。
- 首版只支持照明开关，不操作取暖、换气、烘干或其他功能。

计划交付方式：GitHub 仓库发布通用代码，通过 HACS 安装自定义集成；账户和设备配置只在 Home Assistant 本地录入。
