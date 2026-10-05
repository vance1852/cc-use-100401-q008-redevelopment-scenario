# 深水油田协同运营平台

本项目是一套可离线运行的 Python 服务端平台，用于管理深水油田生产物流、油藏证据评估和关键装备质量。平台把生产节点、输送通道、原油批次、油藏方案、分析决定、装备观测、权限和审计事件持久化到 SQLite，供海上平台、浮式生产储卸装置、油藏团队、装备保障和审计人员协作使用。

## 目录

- src/production_flow/：生产节点、输送通道、原油批次、外输申请、分配和情景分析；
- src/reservoir_assurance/：油藏项目、证据版本、评估协议、观测导入、分析任务与准入决定；
- src/equipment_quality/：装备批次、传感观测、质量分析、账号权限和审批；
- src/scenario_governance/：二次开发情景治理——不可变输入快照、方案比较、版本化投决、实绩偏差与证据追溯；
- fixtures/：离线验收使用的评估协议与结构化观测；
- tests/：领域规则、错误边界、事务、权限、HTTP API、并发批准和命令行验收测试。

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

四条命令会在临时 SQLite 数据库中完成生产生产流转、油藏证据评估、装备质量流程和二次开发情景治理流程，不访问外部网络。

## 二次开发情景治理

情景治理把"流花模式"复制到新油田时的方案批准约束为可重现、可追溯的流程：

1. **不可变输入快照**：储量版本、井组响应、含水预测、设施瓶颈、停产窗口、资本支出、
   油价假设七类输入组成内容寻址（SHA-256）的快照；油藏、工程、财务三方分别对各自
   小节背书，相同内容不重复登记，任何改动只能产生新版本。
2. **方案与比较**：方案只能引用确定的快照摘要；评价（增产、含水产油折减、停产损失、
   NPV、回收期、IRR、低/基准/高油价情景、敏感性区间、平衡油价）全部使用 Decimal
   确定性计算并固化评价算法版本，逐字节可复现；多方案可并排比较但不修改内容。
3. **版本化投决**：批准必须引用确定快照、方案修订版本和要取代的生效投决；
   反对意见随投决永久保留。旧投决不可改写，只能由新批准通过取代链（superseded_by）
   接替；每个油田同时只有一个生效批准，部分唯一索引与乐观并发共同阻止并发双批准。
4. **实绩偏差**：投决后实绩只能按评价年追加，系统生成与预测的偏差报告（10% 容差）；
   超容差只标记"需要新情景"，不得回写旧快照、旧评价或旧投决。
5. **决策追溯**：`/plans/{id}/dossier` 从投决结论回溯快照全部证据与假设、三方背书、
   逐年现金流、敏感性边界、反对意见、实绩偏差和哈希链审计事件。

## HTTP 服务

    PYTHONPATH=src python3 -m production_flow.api --database production-flow.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m reservoir_assurance.api --database reservoir-assurance.sqlite3 --host 127.0.0.1 --port 8081
    PYTHONPATH=src python3 -m equipment_quality.api --database equipment-quality.sqlite3 --host 127.0.0.1 --port 8082
    PYTHONPATH=src python3 -m scenario_governance.api --database scenario-governance.sqlite3 --host 127.0.0.1 --port 8083

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。
