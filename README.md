# 深水油田协同运营平台

本项目是一套可离线运行的 Python 服务端平台，用于管理深水油田生产物流、油藏证据评估、关键装备质量和二次开发情景治理。平台把生产节点、输送通道、原油批次、油藏方案、分析决定、装备观测、输入快照、投决记录、实绩偏差、权限和审计事件持久化到 SQLite，供海上平台、浮式生产储卸装置、油藏团队、装备保障、决策层和审计人员协作使用。

## 目录

- src/production_flow/：生产节点、输送通道、原油批次、外输申请、分配和情景分析；
- src/reservoir_assurance/：油藏项目、证据版本、评估协议、观测导入、分析任务与准入决定；
- src/equipment_quality/：装备批次、传感观测、质量分析、账号权限和审批；
- src/scenario_governance/：二次开发输入贡献、不可变输入快照、方案评价比较、投决修订、反对意见、实绩偏差与追溯；
- fixtures/：离线验收使用的评估协议、结构化观测和二次开发演示输入；
- tests/：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m production_flow.acceptance --workspace .
    PYTHONPATH=src python3 -m reservoir_assurance.acceptance --workspace .
    PYTHONPATH=src python3 -m equipment_quality.acceptance
    PYTHONPATH=src python3 -m scenario_governance.acceptance --workspace .

四条命令会在临时 SQLite 数据库中完成生产流转、油藏证据评估、装备质量流程和二次开发情景治理全流程，不访问外部网络。

## HTTP 服务

    PYTHONPATH=src python3 -m production_flow.api --database production-flow.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m reservoir_assurance.api --database reservoir-assurance.sqlite3 --host 127.0.0.1 --port 8081
    PYTHONPATH=src python3 -m equipment_quality.api --database equipment-quality.sqlite3 --host 127.0.0.1 --port 8082
    PYTHONPATH=src python3 -m scenario_governance.api --database scenario-governance.sqlite3 --host 127.0.0.1 --port 8083

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 二次开发情景治理

针对把流花模式复制到另一座深水老油田的投决场景，治理规则如下：

- 油藏、工程、财务团队分别提交储量版本、井组响应、含水预测、设施瓶颈、停产窗口、资本支出和油价假设七类输入贡献，内容规范化后以 SHA-256 寻址，提交即不可变；
- 七类输入的确定版本组合成不可变输入快照，快照缺少任何一类都会被拒绝；
- 候选方案必须落在指定快照上，确定性评价引擎输出增产、风险评分、回收期、NPV 和敏感性边界（油价 ±20%、井组响应 -20%、资本支出 +20%、盈亏平衡油价），相同输入重放直接返回已存结果；
- 投决必须引用确定的快照摘要、方案摘要和评价版本，反对意见随投决一并留存，也可事后追加（只增不改）；
- 并发批准由乐观修订号和数据库部分唯一索引保证同一项目只有一个有效修订，旧修订以 superseded 状态完整保留，内容列由触发器禁止改写；
- 投产后实绩只生成偏差记录（对照敏感性边界标记是否突破）和派生新情景，旧投决永不改写；
- 决策者可从任一投决追溯全部输入贡献、快照、方案定义、评价结果、敏感性边界、反对意见、偏差和审计事件，审计事件以哈希链串联可校验篡改。
