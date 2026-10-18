# Proteus 1.0.0 实施计划

Proteus 1.0.0 的目标是成为面向 agent 自进化研究的通用框架，提供可复现的方法 recipe、可组合的公共模块，以及统一的执行、评估和实验记录基础设施。用户可以通过配置运行已收录的方法；方法作者可以通过插件加入新机制，复用相同的环境、评估、预算、恢复和报告工具。

首批范围是 GEPA、ACE、Meta-Harness、Combee、CORAL 五个方法家族。现有 Proteus 自进化协议作为 native recipe 保留。这个定位对应“RSI 方向的 ESPnet”：方法实现、任务、运行时和实验配置分别维护，通过 recipe 组合成完整实验。

本文中的新模块、接口、目录和命令均为 1.0.0 的实施目标。未勾选的任务尚未完成。实现状态以代码、契约测试和真实实验记录为准。

### 实施进度（2026-10-17）

- 集成分支：`dev/1.0.0`，从已核实的 main `69f766e73460f67d96a3ce965a5c3f9adaab2f85` 建立。
- 采用 feature branch → 面向 dev 的 PR → 测试和审查 → 合并 dev 的流程；每批汇报审查结果，全部发布验收通过后再向 main 提交集成 PR。
- 第一批 P01：公共对象、严格序列化、JSON Schema、recipe 配置、依赖锁与不可变 RunManifest；实现说明见 [V1_CONTRACTS.md](../V1_CONTRACTS.md)。此批不声称已有新 method executor 或完成方法复现。
- 第二批 P02：注册式 Method/Stage/ActorRuntime、typed ports、角色调用记录、顺序/条件/有限循环、自评 stage 示例与 native 兼容 recipe；说明见 [V1_PLUGINS.md](../V1_PLUGINS.md)。native 复用旧事务引擎，通用 stage 路径不隐式继承 native 提示。
- 第三批拆成 P03a/P03b：P03a 为不可变 ArtifactStore、三类 codec、确定性 delta、候选谱系与 Method 服务接入；P03b 为 pinned TaskSuite/TaskBatch、SubjectRunner、逐任务评估、整批/在线 session 与反馈/显式 JSON 状态协议。两部分共同完成原 P03 的公共服务；本地 Python/受管理 actor 示例不替代 Harbor 或真实方法验收。
- P04–P10、真实方法实验以及 1.0 发布验收继续按本计划推进。当前提供串行模型调用和 task-trial 评估预留；通用持久账本、并发、容器隔离与真实 backend 验收仍需后续批次。

## 1 目标与范围

### 1.1 发布必须实现的能力

- 通过版本化配置运行五个方法家族，并提供各自的参考实现、任务协议和结果。
- 将优化方法、被优化系统、任务集、模型与执行环境解耦。
- 支持顺序阶段、并行批次和持久异步三种执行模式。
- 以 Harbor 和 Docker 作为首个验收的任务执行与评估后端，保留现有 native 执行路径。
- 为每次评估关联准确的候选版本、任务版本、模型设置、evaluator 和初始状态。
- 支持插件扩展、模块替换和消融实验，无须修改核心引擎。
- 保留原有 goal/no-goal、反馈可见性、surface 测量、快照和恢复能力。

### 1.2 首版支持边界

首版覆盖固定模型权重下的 prompt、playbook、记忆、skills、工具和 harness 代码演化。每个 recipe 明确一个已验证的模型、runtime、任务与环境组合；不要求五个方法与所有后端、任务形成完整交叉矩阵。

以下项目放到后续版本：模型训练、多云调度平台、CORAL 多 island 与迁移、任意工作流语言、全量 benchmark 迁移、独立的新仪表盘。现有报告能力随结果 schema 扩展即可。DGM 等其他方法在首批 recipe 完成后接入。

方法覆盖、机制复现和实验结果分别记录。某个方法能在 Proteus 中运行，不代表复现了其全部论文结果；收录多种自进化方法，也不直接构成递归增益或持续复利的实验证据。

## 2 代码基线与迁移原则

设计基线为 upstream main 的 0.4.0，提交 69f766e73460f67d96a3ce965a5c3f9adaab2f85。实施开始时重新核对 main，并记录最终采用的开发基线。

计划最初所在工作树早于该基线。实施已使用从该基线建立的独立工作树，原工作树的报告和其他未提交文件保留。后续继续以集成分支实际代码为准，避免重复实现已有功能。

| 已有能力 | 1.0.0 的处理方式 |
| --- | --- |
| HarnessAdapter、Surface、归一化 ActionEvent | 保留兼容接入层，扩展为 actor、subject 和 artifact 的明确边界 |
| 自定义阶段顺序、提示和阶段预算 | 作为 native recipe 基础，升级为带输入输出的 Stage 和执行算子 |
| 快照、候选保留、回滚、运行锁与恢复 | 提取公共事务服务；扩展至多候选和方法状态 |
| GoalConfig、EvaluatorSpec、反馈可见性与调度 | 保留实验控制，接入新的逐任务评估结果和证据视图 |
| DockerSandbox、环境 manifest、source-mode 构建与验证 | 保留已有路径，新增 Harbor 接入并逐步统一环境声明 |
| BenchTask 与 grader | 保留旧实验语义；新增执行指定候选的 SubjectRunner |
| 测量、审计、报告、发布 smoke | 保留，并明确哪些 artifact 类型和谱系支持哪些测量 |

现有 BenchTask 对持续存在的 task workspace 打分；新 SubjectRunner 要在明确的初始状态下执行指定候选。两者分别保留，避免一次实验从“评价已完成的任务成果”静默变成“重新执行候选系统”。

现有 native 协议的阶段提示、epistemic protocol、disposition 与上下文新建规则，只由选择该协议的 recipe 启用。其他方法使用其明确声明的提示、反馈时机和状态规则。

## 3 模块与职责

~~~mermaid
flowchart TD
    R[Recipe 配置与版本锁] --> M[Method 方法控制流]
    M --> O[Stage 与执行算子]
    O --> A[ActorRuntime 优化者与其他角色]
    O --> S[SubjectRunner 候选系统执行]
    S --> E[TaskSuite 与 EvaluationService]
    S --> H[EnvironmentBackend 与 Harbor]
    M --> C[ArtifactStore Archive 搜索策略]
    O --> K[EvidenceStore 与 SharedKnowledgeStore]
    M --> N[RunEngine 预算事件检查点恢复]
~~~

### 3.1 四个必须独立的概念

**优化者与被优化系统。** 优化者可以是 coding agent，也可以是反思或整理角色；被优化系统是在任务上接受评估的候选。两者允许使用相同模型，但其 runtime、权限、状态与预算必须能分别配置。native 自演化是二者通过明确版本关系连接的一种方法。

**执行与评分。** SubjectRunner 负责候选加载、任务执行和 rollout；EvaluationService 负责评分与反馈。Harbor 可以执行完整 trial，Proteus adapter 将其中的执行与评分结果映射到这两个契约，不重复评分或重复计算用量。

**候选保留、parent 选择与最终选择。** 三者分别由 ArchivePolicy、ParentSelector、FinalSelector 负责。低平均分候选可以因局部优势被保留；被保留不等于被部署。方法也可以不使用自动 parent selector。

**状态类型。** Candidate artifact、运行中的 session、MethodState、框架 handoff、共享研究知识分别保存。框架生成的 handoff 不能被计作方法学习到的记忆。

### 3.2 公共数据契约

| 对象 | 最小内容 |
| --- | --- |
| ArtifactRef | 内容 hash、codec/schema 版本、组件 manifest、加载所需的 runtime/environment 引用 |
| Candidate | ID、0 到多个 parent ID、artifact 引用、创建操作、验证状态；启发来源可另记为 evidence 引用 |
| Rollout | candidate ID、task ID、序列与步骤位置、输出、轨迹引用、状态、实际 runtime 与用量、运行状态前后引用 |
| EvaluationBatch | 对齐的 task ID、逐任务分数与指标、反馈引用、evaluator 版本、状态与评估协议 hash |
| StageRequest / StageResult | 操作 ID、算子与角色、输入输出引用、读写范围、预算、attempt ID 和完成状态 |
| MethodState | 方法私有的版本化状态、archive 引用、采样游标、随机状态、待完成操作与 checkpoint 游标 |
| RunManifest | 解析后的 recipe、源码和依赖版本、提示 hash、模型设置、数据划分、环境、预算与状态协议 |

核心只解释公共字段。方法特有 payload 使用注册的 schema 与 codec，避免在核心写入 ACE、GEPA 或 CORAL 的专有字段。

### 3.3 插件接口草案

以下是职责层面的签名；具体 async 边界和类型在 I01、I02 中冻结。

~~~text
Method.run(services, restored_state) -> MethodResult
Stage.run(context, inputs) -> StageResult
ActorRuntime.execute(actor_spec, request) -> ActorResult
ArtifactStore.apply_delta(base_ref, delta, codec) -> ArtifactRef
SubjectRunner.run(candidate, task_batch, execution_spec) -> RolloutBatch
EvaluationService.evaluate(rollouts, evaluation_spec) -> EvaluationBatch
ParentSelector.select(archive_view, state) -> ParentSelection
ArchivePolicy.update(archive_view, candidates, evaluations) -> ArchiveUpdate
FinalSelector.select(archive_view, evaluation_protocol) -> CandidateSet
~~~

方法控制流允许用 Python 插件实现。外部执行、模型调用和正式评估必须经过公共服务，以便记录用量、检查预算、持久化结果和恢复。框架不能只启动一个不透明的外部脚本、最后读取其最高分，就把它视为完整 recipe。

Stage 至少支持模型调用、coding-agent 操作、确定性产物更新、subject rollout 和评估。新增 self_evaluator stage 可以返回自评证据；是否参与候选选择由 recipe 另行声明，不能自动替换正式 evaluator。

## 4 公共基建行动项

I01 到 I15 均属于 1.0.0 发布范围。实现可以分批落地；已有部分通过兼容层复用。

### I01 统一数据契约

- [x] 定义第 3 节对象的类型、JSON schema、状态枚举与 schema version（P01）。
- [x] 建立 candidate、rollout、evaluation、operation、run 之间的引用约束（P01 完整记录集校验；执行时接入由 P02–P04 完成）。
- [x] 区分有效任务失败、候选无效、执行超时、基础设施错误、评分缺失；不可用分数不自动作为有效零分（P01 数据契约）。
- [x] 定义插件状态序列化与版本迁移接口，拒绝无法解释的状态版本（P01 StateRegistry；真实方法状态随后注册）。

**验收：** 公共对象可序列化往返；错配候选、任务、评分长度或版本会被检测；所有后续组件使用相同契约。

### I02 Method 和 Stage 插件

- [x] 建立方法与算子注册机制，支持方法私有命名空间（P02 显式注册、options/data/state schema、运行时注册表快照）。
- [x] Stage 声明输入、输出、角色、工具、读写范围、失败处理和预算（P02 校验数据端口、能力与调用预留；Python 插件的声明不等同于进程权限沙箱）。
- [x] 支持顺序执行、条件决策、有限循环、多候选输出及方法主动提交评估（P02 控制流与多输出；P03b `evaluate`/`open_evaluation` 正式候选评估服务）。
- [x] 把现有循环接成 native recipe，保留旧 CLI 的兼容入口（P02 覆盖 minimal/llm/dsh/pi/codex 绑定；旧 CLI 继续覆盖原有高级参数和 adapter）。
- [ ] 将 native 提示和控制条件与公共基础设施分开，记录最终渲染的提示版本（P02 已隔离 native 协议并记录 core-rendered phase prompts；adapter 追加的最终消息仍需按 backend 补全记录）。

**验收：** 新增一个 stage 插件和配置即可运行；方法能够改变阶段组织而不修改 engine；原有 native 事务与恢复检查继续通过。

### I03 角色与运行时

- [x] 定义 ActorSpec，分别配置角色的模型、runtime、提示、工具、session 策略与预算（P01/P02；无法兑现的非默认配置提前拒绝）。
- [ ] 接入一个通用 LLM runtime 和一个真实 coding-agent runtime；既有 harness adapter 通过兼容层保留（P02 已有 chat HTTP transport 和 coding harness bridge；真实模型/容器验收随 M1 进行）。
- [x] 将执行能力声明为可检查的 capability，如原生 resume、候选代码加载、归一化轨迹（P02 registry 和 lock；能力由后端实现声明并按配置检查）。
- [ ] 明确 fresh、handoff、native session 三种上下文策略，不做隐式互换（P02 已区分请求类型及拒绝不匹配；通用内置 actor 当前为 fresh，native compatibility 按原 adapter 的策略执行；持久 session 在 I11 完成）。

**验收：** 优化者与 subject 可以使用不同配置；同一 backend 可承接不同角色；缺少 recipe 所需能力时在正式调用前报错。

### I04 候选与产物存储

- [x] 提供 text components、structured records、file tree 三类基础 codec（P03a）。
- [x] 实现 fork、apply_delta、validate、materialize，以及不可变 manifest 和内容 hash（P03a；validate 检查字节与 codec 完整性，runtime build/boot 属于下一项）。
- [x] 产物更新由确定性算子执行；LLM 负责提出 delta 或 patch（P03a delta 以父 hash 为前置条件，服务保留提案与失败操作记录）。
- [x] 保存被拒绝与失败候选；通过元数据区分原生 episode checkpoint 与通用 candidate 谱系（P03a 候选和决定分开持久化，checkpoint import 明确标记来源）。
- [ ] 复用 source-mode 校验与精确树加载，检查删除、重命名及构建缓存失效（P03a 已验证全新目录物化、删除/重命名及 Python 缓存隔离；真实 source-mode/Harbor 加载与 build/boot 由 P04 接入）。

**验收：** 指定版本可重新加载；更新不修改 parent；失败候选可检查和按方法规则恢复；实际运行的代码与记录的候选一致。

### I05 任务集与候选评估

- [x] 定义 TaskSuite、TaskBatch 和 ExecutionSpec，声明数据版本、split、顺序及 seed（P03b 强制执行时校验 dataset 内容 hash、version、split、任务集合和初始状态）。
- [x] 实现 SubjectRunner，运行指定候选并保存逐任务输出与轨迹（P03b 本地 Python/受管理 prompt actor，检查实际加载 hash 和 runtime/environment；Harbor runner 在 I06）。
- [x] 支持独立任务重置、序列内状态继承、候选间相同起点三种状态规则（P03b 显式 JSON 状态/历史、新 runner/actor 实例；每个独立评估 session 重置起点，online session 可在保留显式状态时更换候选；持久 filesystem/worker 状态见 I06/I11）。
- [x] EvaluationService 保留逐样本指标、反馈和错误，不只保留平均分（P03b 完整任务 cohort 与不可用状态；不缩小分母，隐藏数据不进入公共 Values/历史）。
- [x] online recipe 必须先记录预测，再接收允许的反馈并更新；保留协议要求的时间顺序（P03b prediction commit → allowed feedback → explicit state/candidate update；落盘失败停止重试且不推进状态）。
- [x] 定义缓存键，覆盖候选、任务、模型设置、evaluator、初始状态、环境和随机条件；序列任务另含历史与位置（P03b 同时包含依赖锁和实现身份；本批每次重执行，尚不复用缓存结果）。

**验收：** baseline 和修改候选能独立重跑同一批任务；任务或状态不串用；隐藏评分不会通过提示、缓存或历史视图进入优化者上下文。

P03b 已验证本地可信插件的 API/数据流及显式 JSON 状态边界。该后端不是针对恶意代码的 filesystem 沙箱；不可托信 agent 的容器与独立 verifier 隔离仍必须通过 I06/P04 验收。

### I06 环境与 Harbor

- [ ] 定义 EnvironmentSpec，声明镜像或构建定义、工具链、资源、网络、访问范围和状态生命周期。
- [ ] 实现 HarborSubjectRunner 与 candidate-aware agent adapter，使用锁定的 Harbor 版本。
- [ ] 将 candidate ID、内容 hash、Harbor trial ID、task checksum 和实际加载版本关联到同一结果。
- [ ] 映射 Harbor 的 reward、轨迹、用量、超时和异常；保留原始结果引用。
- [ ] 支持 trial、sequence、worker 三种生命周期，并检查底层 provider 的实际能力。
- [ ] 需要独立 grader 的 recipe 显式使用 separate verifier，并声明传递的产物；不假设默认 verifier 已独立。
- [ ] 镜像记录实际 digest，构建状态与方法产物分开；镜像和缓存不携带其他候选的学习状态。
- [ ] Harbor 作为可选依赖或独立受控运行时接入，处理其 Python 要求与 Proteus 核心支持版本的差异。

**验收：** 在 Docker 中运行两个不同候选并确认实际执行版本；结果与状态隔离、超时、清理和资源用量有记录；不把缺少 Docker 的运行静默替换成宿主机执行。

### I07 Archive 与搜索策略

- [ ] 建立候选和评估的可查询 archive；支持多分支及多个 parent 引用。
- [ ] 分离 ParentSelector、ArchivePolicy、FinalSelector。
- [ ] 按参考版本实现 GEPA 逐样本 frontier 和 parent 选择；提供标量及多目标策略供其他 recipe 使用。
- [ ] 支持不使用自动 parent selection 的 agent-directed proposal。
- [ ] 保存采样状态、接纳与拒绝原因；最终结果可为候选集合或 frontier。

**验收：** 固定评分矩阵和 seed 的策略结果可核对；局部优势候选不会因标量平均而被错误删除；最终选择不读取未声明的测试结果。

### I08 证据与共享知识

- [ ] EvidenceStore 保存代码、rollout、评分、诊断和来源关系，提供可检索的文件视图。
- [ ] EvidenceView 按角色、split、反馈时机和权限选择证据。
- [ ] SharedKnowledgeStore 支持 attempts、notes、skills 等带来源和版本的产物。
- [ ] 区分优化者的工作 handoff、方法演化产物、共享研究知识和私有评估记录。
- [ ] 并发写入使用原子提交或版本冲突检测；不能仅假设 agent 总会选不同文件名。

**验收：** Meta-Harness proposer 能按需检索多个历史候选；多个 worker 能交流知识且保留来源；无权访问的评估内容不出现在可见视图中。

### I09 预算事件与恢复

- [ ] 建立运行事件和用量账本，记录 operation、attempt、stage、worker 和 trial。
- [ ] 汇总优化者、反思者、curator、subject、grader、profiling 与重试的实际用量。
- [ ] 并发调用预留预算并结算，未返回的用量保持 unknown，不能记录成零。
- [ ] 检查点包含 artifact、archive、MethodState、采样游标、随机状态、配置 hash 及待完成操作。
- [ ] 恢复复用已提交结果；不重复提交已完成的评估，不补发预算；外部副作用不确定时记录 attempt 并使用明确的恢复策略。
- [ ] 保留 native 的运行锁、候选修复和配置一致性检查。

**验收：** 在候选生成、评估完成与结果提交等边界注入中断，能够恢复且账本一致；并发预算预留有效；修改实验条件不会伪装成续跑。

### I10 并行执行与聚合

- [ ] 提供受预算约束的 map、join 和分层聚合，避免两个调度层分别扩大并发。
- [ ] 保存分组、shuffle seed、复制关系、聚合树和每次聚合的输入版本。
- [ ] 聚合使用基础方法的更新语义，保留原始 reflection 的来源 ID；复制不增加真实任务样本计数。
- [ ] 实现 profiling 与 BatchController，把 profiling 开销计入总预算。
- [ ] 恢复时复用已完成分支；取消与失败处理有明确的传播规则。

**验收：** 同一批 reflection 能按记录的分组与树结构重放；中断不丢分支；不以 LLM 聚合满足严格结合律或重新调用可得到完全相同文本为前提。

### I11 持久 actor 与事件干预

- [ ] 管理 worker 的启动、session 继续、暂停、停止及异常退出恢复。
- [ ] 每个 worker 有独立代码工作区、身份和预算记录，共享知识通过 I08 访问。
- [ ] 提供正式 EvaluationGateway，让 agent 主动提交准确版本的候选并获得允许的反馈。
- [ ] EventPolicy 支持评估次数、时间和停滞触发；将 reflect、consolidate、pivot 作为 CORAL 插件策略。
- [ ] 保存触发历史、session 引用与恢复方式；无法恢复原生 session 时显式记录降级，不假称续接了原上下文。

**验收：** 至少两个真实 worker 异步探索并共享知识；评估和干预不重复结算；重启后能按声明的 session 协议继续工作。

### I12 Recipe 配置与注册

- [x] 定义版本化配置，组合方法、阶段、角色、subject、任务、环境、搜索和预算（P01；插件专有 options 校验由 P02 注册）。
- [x] 配置解析后生成不可变 RunManifest 和 dependency lock（P01 接受显式解析的版本锁；实际运行环境采集随后接入）。
- [x] 提供默认值与覆盖规则；解析后的配置可直接导出，未知字段和缺少 capability 提前报错（P02 registry 解析插件 options 和端口；`recipe check` 导出完整配置）。
- [ ] 将 paper reference、code reference、实现状态、偏差和结果证据纳入 recipe manifest。
- [ ] 参考复现配置与研究扩展配置分别命名，更新公共模块不静默改变已锁定 recipe。

**验收：** 已收录方法可通过配置运行；第三方插件可注册；配置改变能形成独立实验并被比较。

### I13 研究者入口与模板

- [ ] 提供 recipe 列举、检查、运行、恢复和导出入口。
- [ ] 提供新 method、stage、evaluator、subject adapter 的最小模板及 capability 测试。
- [ ] 提供快速离线示例，以及一个真实任务的端到端示例。
- [ ] 给出新增 self_evaluator stage、替换 search policy 的教程。

**验收：** 研究者按教程添加插件与配置即可完成两种扩展，不修改核心代码；安装包中的模板在仓库外可用。

### I14 统一实验输出

- [ ] 输出逐任务指标、候选谱系、阶段与角色成本、耗时、失败与重试统计。
- [ ] 提供 baseline、模块消融与多 seed 实验配置和汇总。
- [ ] 分别保存参考设置复现结果与共同实验条件下的方法比较结果。
- [ ] Combee 报告质量、成本与 wall-clock 的权衡；CORAL 报告单 worker 与多 worker 条件的预算及共享行为。
- [ ] 保留原有测量工具；仅对支持的 artifact 和 lineage 计算对应测量，不把不同对象强行合成可比较的结构距离。

**验收：** 结果能由已保存的逐任务记录重新汇总；每张结果表能追溯到配置、候选与原始输出；缺失指标明确显示。

### I15 兼容文档与发布

- [ ] 定义 1.0 稳定公共 API、schema 和插件版本兼容规则。
- [ ] 保留旧 CLI 的兼容入口；记录旧 run 的读取、续跑与迁移支持边界。
- [ ] 不改写旧实验记录；不兼容的续跑要求原版本完成或新建 run。
- [ ] 更新架构、recipe、扩展、Harbor、结果解释、迁移与发布文档。
- [ ] 保留既有 runtime 发布检查，并增加新 recipe 的端到端检查。
- [ ] 验证 wheel、sdist、可选依赖、离线模板与真实 backend 的安装路径。

**验收：** 新环境能按文档安装、运行、查看结果、恢复和导出；发布产物与验证的同一提交一致。

## 5 五个方法家族的 recipe

所有方法先冻结参考版本和设置，再实现与核对机制。允许直接接入上游算法，但候选执行、用量、产物、状态和结果仍须进入 Proteus 公共服务。方法特有逻辑保留在方法插件，经过多个 recipe 使用后再抽取公共模块。

### R01 GEPA

**流程：** 选择 parent → 采样任务与待修改组件 → 获取轨迹和反馈 → 反思修改 → 按参考规则比较 → 更新候选集合。最终配置同时声明组合操作是否启用。

- [ ] 锁定论文与实现版本、反思提示、数据划分和搜索预算。
- [ ] 优先通过上游 adapter 和回调接入算法，桥接逐样本评估、候选与状态持久化。
- [ ] 核对 parent 选择、minibatch 比较、验证策略、组件修改及参考版本的组合操作。
- [ ] 在固定评分与 proposal fixture 上对照参考逻辑，再运行真实 benchmark。

**依赖：** I01–I07、I09、I12。**完成定义：** 有一个完整评估划分的真实结果、可恢复的候选搜索记录和逐项机制对照。

### R02 ACE

**流程：** 当前 playbook 执行任务 → 获得允许的反馈 → reflector 提取经验 → curator 提出 delta → 确定性更新 → 按规则 refine。

- [ ] 定义版本化 playbook codec、条目 ID、helpful/harmful 计数与操作 schema。
- [ ] 分别配置 generator、reflector、curator；保留参考提示和调用顺序。
- [ ] 实现 offline 与 online 协议，锁定顺序、epoch、反馈来源和更新时点。
- [ ] 核对官方代码实际支持的操作；论文描述与代码差异进入偏差清单，额外 UPDATE/MERGE 等能力单独作为扩展 profile。
- [ ] 验证固定 delta 的确定性、条目保留及在线预测前后时序。

**依赖：** I01–I06、I08、I09、I12。**完成定义：** playbook 与每次更新可追溯；有完整声明协议下的真实结果及 baseline。

### R03 Meta-Harness

**流程：** proposer 按需检索历史代码、分数和轨迹 → 提出一个或多个 harness → 接口验证 → 执行评估 → 保存历史 → 继续搜索。

- [ ] 为 proposer 提供可检索的历史文件视图，不强制压缩成单一摘要。
- [ ] 实现领域 subject 的接口契约与候选加载，允许一次提交多个候选。
- [ ] 不施加原方法没有的 parent sampling 规则；proposer 可自由参考多个历史版本。
- [ ] 保留有效候选和失败诊断，按照声明的指标选择最终候选或 frontier。
- [ ] 锁定并运行一个原方法领域的真实任务设置。

**依赖：** I01–I09、I12。**完成定义：** proposer 的历史检索、候选生成及后续评估均可审计；最终选择遵守数据划分。

### R04 Combee

Combee 实现为可组合的并行学习层，交付 combee_ace 与 combee_gepa 两个配置，不复制两份基础方法。

- [ ] 定义 reflection 到 update 的聚合接口，复用基础方法的产物与更新语义。
- [ ] 实现 augmented shuffle、parallel scan aggregation 和动态 batch controller。
- [ ] 保存复制、分组、层次与提示顺序，统计所有聚合及 profiling 调用。
- [ ] 用固定 trace 集比较顺序基础方法、naive parallel 和 Combee；包含固定 batch 与动态 batch 条件。
- [ ] 对应实验报告质量、成本和实际耗时；离线 trace 学习与在线 rollout 条件分开。

**依赖：** R01、R02、I10、I12、I14。**完成定义：** 两个组合都可运行；至少一个真实设置有完整权衡结果，动态控制器已实现并验证。

### R05 CORAL

首版范围为论文核心的单共享空间异步多 agent：长期探索、共享 attempts/notes/skills，以及 heartbeat 干预。多 island 与迁移留作后续配置。

- [ ] 多个 worker 独立维护工作区与 session，通过共享知识交流发现。
- [ ] 通过 EvaluationGateway 提交候选，允许 agent 决定探索、检索和评估时机。
- [ ] 实现参考条件中的 reflect、consolidate、pivot 及触发规则。
- [ ] 实现可恢复的 worker 生命周期、全局预算预留和共享写入处理。
- [ ] 选取不涉及模型训练的原论文优化任务，完成单 worker 对照和至少两个 worker 的实验。

**依赖：** I01–I09、I11、I12、I14。**完成定义：** 真实异步协作、共享知识、触发干预和中断恢复都有运行记录；不能仅以成功启动多个进程作为完成。

### R00 Native

- [x] 将现有协议包装为兼容 recipe，保留配置、反馈、测量和事务语义（P02 复用原 RunConfig 引擎，旧入口继续可用；live release gates 仍待后续验收）。
- [ ] 继续验证固定 active 与 writable candidate、边界校验、失败候选修复和恢复。
- [ ] 明确每个 adapter 的能力；没有 staged activation 的 adapter 不获得该保证。

Native 不计入五个外部方法家族的完成数量。

## 6 Recipe 包与开发者入口

### 6.1 每个实验包的必要文件

- 方法配置、角色提示、插件依赖锁和参考版本。
- 起始 subject、可修改范围、固定部分与环境定义。
- 任务版本、split、顺序、状态重置和反馈协议。
- 预算、停止条件、失败与重试规则及 baseline。
- 运行说明、结果汇总脚本、机制核对清单与偏差说明。

建议的目录划分如下。基础方法插件、公共组件、任务适配与实验 recipe 各自维护；模块位置可在 M0 的 API 设计中调整，但职责不合并。

~~~text
proteus/
  core/               公共契约、事务、事件、预算、恢复
  methods/            native、gepa、ace、meta_harness、coral
  operators/          通用 stage 算子与 Combee 聚合组件
  execution/          actor、subject、environment、Harbor 接入
  stores/             artifacts、archive、evidence、shared knowledge
  policies/           选择、保留、batch、事件策略
recipes/
  native/
  gepa/
  ace/
  meta_harness/
  combee_ace/
  combee_gepa/
  coral/
~~~

### 6.2 配置草案

以下配置展示职责分离，不是当前可执行命令或已经冻结的 schema。candidate 的不可变 ID 在运行时产生，写入 RunManifest 与 operation 记录。

~~~yaml
schema_version: 1
recipe: ace_formula_reference
method:
  plugin: proteus.methods.ace:ACE
  reference_profile: reference_v1
actors:
  generator: {runtime: llm, model: "${SUBJECT_MODEL}"}
  reflector: {runtime: llm, model: "${REFLECTOR_MODEL}"}
  curator: {runtime: llm, model: "${CURATOR_MODEL}"}
subject:
  plugin: project.formula:PlaybookSubject
  initial_artifact: artifacts/initial-playbook.json
task_suite:
  plugin: project.formula:FormulaSuite
  protocol: configs/offline-protocol.yaml
execution:
  backend: harbor
  provider: docker
  environment: environments/formula/task.toml
  state_scope: trial
budget:
  max_model_calls: 200
  max_evaluations: 100
  max_concurrency: 4
seed: 0
~~~

示例中的数值仅展示配置字段；正式 recipe 的预算、模型和协议在 M0 冻结。无容器需求的操作可以直接执行，任务执行后端由 recipe 声明。

拟议 CLI 为 recipe list、check、run、resume、export，以及插件 scaffold。具体拼写在 I13 实现时统一，不把文档中的示意命令当作现有功能。

## 7 里程碑与依赖

不在缺少执行资源和模型预算时预设工期。每个里程碑按可验证的交付物完成，先打通纵向实验，再扩展公共抽象。

| 里程碑 | 工作范围 | 可验收交付物 |
| --- | --- | --- |
| M0 实施规格 | 冻结基线、公共 schema、参考版本、首批 benchmark 和预算；建立支持矩阵 | 实现任务拆分、契约草案、每个 recipe 的参考设置与偏差清单 |
| M1 公共闭环 | I01–I06、I09、I12 最小实现，R00 | 两个候选独立执行、逐任务比较、结果导出与中断恢复 |
| M2 GEPA | I07 与 R01，接入上游算法 | 机制对照和一个完整真实实验 |
| M3 ACE 与 Meta-Harness | I08、R02、R03 | 结构化更新、任务流、多角色、跨候选历史检索及各自真实实验 |
| M4 Combee 与 CORAL | I10、I11、R04、R05 | 并行学习权衡实验、异步共享知识实验与恢复检查 |
| M5 研究者工具与发布 | I13–I15 完整实现及全套回归 | 扩展教程、实验包、支持矩阵、迁移文档和发布候选 |

I09、I12 从 M1 开始随每个方法扩展，不能拖到所有方法完成后才补记录和预算。I13–I15 的基础入口和文档同步建设，M5 完成最终验收。

### 7.1 建议的实现 PR 顺序

1. P01 公共 schema、序列化、配置与 RunManifest。
2. P02 Method/Stage、角色接口和 native 兼容层。
3. P03a ArtifactStore、codec 与候选操作；P03b TaskSuite、SubjectRunner 与逐任务评估（保留原 P03 完整范围）。
4. P04 Harbor bridge、候选精确加载、事件账本与闭环恢复。
5. P05 Archive/search policies 与 GEPA recipe。
6. P06 Structured records、delta 算子与 ACE recipe。
7. P07 Evidence views、代码 subject 与 Meta-Harness recipe。
8. P08 并行执行、聚合、batch controller 与 Combee recipes。
9. P09 持久 worker、共享知识、事件干预与 CORAL recipe。
10. P10 扩展模板、汇总工具、迁移与发布检查收尾。

每个 PR 带对应的契约检查与最小使用说明。真实方法实验随 recipe 完成，不集中到最后才发现接口不足。

## 8 验证与实验要求

### 8.1 公共契约与集成检查

- 候选版本和实际执行版本一致，包含文件删除、重命名及缓存失效。
- 独立任务可重置；序列按规定继承状态；不同候选不继承另一候选的运行状态。
- artifact、MethodState、session、handoff 与共享知识边界正确。
- 可见证据与隐藏评估分离，反馈时序符合 recipe。
- 在调用中断、评估完成但未提交、并行分支完成和 worker 重启等边界测试恢复。
- 同时启动多个操作时不突破预算预留，失败和重试计入用量。
- 原有 staged adapter、配置一致性、运行锁、快照与发布检查不回退。

### 8.2 方法一致性检查

| 方法 | 必须核对的语义 |
| --- | --- |
| GEPA | 固定评分矩阵与 seed 下的 frontier、parent、接纳和组件选择；按参考 profile 核对组合操作 |
| ACE | 固定 delta 的条目 ID 与计数结果；更新顺序；online 的预测先于更新 |
| Meta-Harness | proposer 访问的历史、一次多个候选、有效性检查、最终选择与数据划分 |
| Combee | shuffle 来源与分组、聚合树、基础更新语义、profiling 和动态 batch 决策 |
| CORAL | 工作区独立、共享知识来源、正式评估提交、heartbeat 时序、session 继续与恢复 |

### 8.3 实验结果的完成标准

每个方法家族至少完成一个真实 benchmark 设置及其声明的完整评估划分。Combee 的两种组合都要验证可运行，至少一种组合交付完整质量、成本与耗时对照；CORAL 包含单 worker 与多 worker 条件。

每份结果记录参考版本、模型设置、baseline、任务 split、seed、预算、环境、最终选择规则、失败处理和已知偏差。提供多 seed 执行与汇总能力，发布结果注明实际完成的 seed 和波动统计，不用单次 smoke 结果代替稳定性证据。

参考设置复现与共同条件下比较使用不同配置。调整模型、任务或预算后，结果只支持对应的新设置；1.0 不要求每个方法达到论文报告的同一数值，但必须报告结果与差异，而不能只交付一个具有相似名称的流程。

### 8.4 扩展性验收

- [ ] 在不修改核心代码的情况下加入一个 self_evaluator stage，输出单独的证据类型。
- [ ] 在不修改核心代码的情况下替换一个 ParentSelector 或 ArchivePolicy，完成对照实验。
- [ ] 用另一个 actor runtime 跑通至少一个相同 recipe，验证公共接口而非仅有单一实现。
- [ ] 从安装包生成第三方插件模板，在仓库外完成运行和结果导出。

## 9 发布清单

- [ ] I01–I15 的验收记录齐全，R00 与 R01–R05 均完成对应范围。
- [ ] 每个 recipe 有版本锁、运行说明、机制核对、支持矩阵、真实结果与偏差说明。
- [ ] 公共 API 与配置 schema 定义稳定，插件扩展教程通过实际验证。
- [ ] 原有功能回归通过；不支持的 adapter 能力被明确拒绝或限制。
- [ ] 全局预算、检查点与恢复验证通过；持久 session 降级有记录。
- [ ] 已保存的逐任务记录可重新汇总为报告中的表与曲线。
- [ ] wheel、sdist 和可选依赖在新环境安装验证通过。
- [ ] README、架构、recipe、迁移与 1.0.0 release notes 同步。
- [ ] 对实际发布提交运行既有和新增的 release gates；需要的 live 检查不以静默 skip 代替。
- [ ] 设置版本、打 tag 和发布按仓库发布流程执行；计划文档落地不代表已经发布 1.0.0。

## 10 实施开始时需要冻结的参数

以下事项不影响先完成公共设计，但必须在对应真实实验启动前写入配置与预算记录。

| 参数 | 冻结位置 | 要求 |
| --- | --- | --- |
| 方法参考实现与 commit | M0 recipe manifest | 同时记录论文版本与代码版本，区别 paper/reference/extended profile |
| 首批任务与 baseline | M0 TaskSuite 配置 | 优先有原方法对应的公开任务；单列共同实验条件 |
| 模型、runtime 与推理设置 | ActorSpec 和 ExecutionSpec | 优化者与 subject 分开，记录可解析的实际版本 |
| Harbor、镜像与工具链 | 环境 lock | 首个正式验收环境为支持对应任务的 Linux Docker 组合 |
| seed、split、更新和重置时序 | EvaluationProtocol | offline、online、sequence 与 worker 的状态协议明确 |
| 调用、评估、成本与并发上限 | RunManifest | 包含 profiling、重试和所有角色，符合可用执行资源 |
| 对外支持的 API 与旧配置范围 | I15 兼容文档 | 不把所有内部类都承诺为稳定插件 API |

## 11 参考资料

### Proteus 基线

- [0.4.0 代码基线](https://github.com/proteus-evolve/Proteus/tree/69f766e73460f67d96a3ce965a5c3f9adaab2f85)
- [Episode 协议与适配器能力](https://github.com/proteus-evolve/Proteus/blob/69f766e73460f67d96a3ce965a5c3f9adaab2f85/docs/EPISODE.md)
- [现有 benchmark 契约](https://github.com/proteus-evolve/Proteus/blob/69f766e73460f67d96a3ce965a5c3f9adaab2f85/proteus/bench/task.py)
- [现有发布流程](https://github.com/proteus-evolve/Proteus/blob/69f766e73460f67d96a3ce965a5c3f9adaab2f85/docs/RELEASING.md)

### 方法与执行后端

- [GEPA 论文](https://arxiv.org/abs/2507.19457)；[官方实现](https://github.com/gepa-ai/gepa)
- [ACE 论文](https://arxiv.org/abs/2510.04618)；[官方实现](https://github.com/ace-agent/ace)
- [Meta-Harness 论文](https://arxiv.org/abs/2603.28052)；[官方实现](https://github.com/stanford-iris-lab/meta-harness)
- [Combee v2 论文](https://arxiv.org/abs/2604.04247v2)
- [CORAL v3 论文](https://arxiv.org/abs/2604.01658v3)；[官方实现](https://github.com/Human-Agent-Society/CORAL)
- [Harbor custom agents](https://docs.harborframework.com/agents/custom-agents)
- [Harbor sandboxes](https://docs.harborframework.com/sandboxes/pre-integrated-sandboxes)
- [Harbor multi-step 与 session resume](https://docs.harborframework.com/tasks/multi-step)
- [Harbor separate verifier](https://docs.harborframework.com/tasks/separate-verifier)

这些链接用于定位方法和接口。正式 recipe 以 M0 锁定的版本和实验配置为准，不能因上游 main 更新而静默改变复现目标。
