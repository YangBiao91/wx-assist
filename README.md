# wx-assist · 本地单群优惠提醒扩展

本仓库源自 [MaleleStudySpace/wx-assist（摘星）](https://github.com/MaleleStudySpace/wx-assist)，本分支基于上游 **v1.9.0**（`5d4b2a0d3b2911e06a717f7aafc2daa19e5bb639`）整理。保留上游 Git 历史、MIT 许可证和原作者署名。上游项目介绍保存在 [README.upstream.md](README.upstream.md)。

新增的 [monitor/](monitor/README.md) 是 Windows 本地单群蛋白粉优惠监控：

- ON 单件 5 磅、人民币报价严格低于 580 元。
- 熊猫单件 5 磅、人民币报价严格低于 400 元。
- “超好价”及已配置的特殊字／拆字写法独立提醒。
- 增量读取、SQLite 事务与去重、Windows 通知、经典 Outlook 邮件队列、登录自启与故障恢复。

**先阅读 [监控安装、配置、测试与实现说明](monitor/README.md)。** 邮件默认关闭，群、邮箱和微信凭据只在本地配置。仓库不包含真实密钥、群标识、聊天内容、候选数据库、个人邮箱或实发测试报告。

监控入口直接调用经过修改的只读数据库模块，不启动上游桌面 UI、Bot、AI、RAG、HTTP 服务或密钥提取组件。上游代码的修改仅涉及监控必需的数据库内存快照与只读处理；上游完整应用的其他入口保留原有行为，不能将整个上游应用视为仅具监控功能。

本分支不重新分发上游发布包内未公开源码的组件，不提供捆绑 DLL／WASM／EXE。上游代码及新增代码遵循仓库 [MIT License](LICENSE)。
