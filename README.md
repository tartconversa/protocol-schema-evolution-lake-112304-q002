# 跨实验室方案版本迁移与事实表兼容

本仓库把“协议登记 → 演进装载 → 兼容数据集生成 → 版本追溯”做成一条可重复、
可审计的流程，面向多个实验室共享同一类实验结果、但方案版本不断演进的场景。

所有数据均为本地合成资料，不包含真实实验室、研究人员或实验结果。仅使用
Python 3.11–3.14 标准库，无需外部服务。

## 核心立场

数据平台需要交付“跨版本可比较的数据产品”，同时不能抹掉旧实验的原始语义。
因此本项目不把“新版本多了一个字段”“名字改了”“单位变了”“字段被拆开”一律
当成兼容变更，而是要求每条跨版本合并同时具备三类依据：

1. **物理证据**：字段名称与单位必须支持映射表的声明。声明 `same` 却单位不同，
   或声明 `unit-convert` 却连字段名都不同，一律判为 `claim_contradicted`。
2. **治理审批**：映射记录必须为 `approved`；同一映射的重复记录若互相冲突，
   在获得正式裁定前不得自动合并。
3. **语义评审**：实验负责人必须给出“科学含义是否改变”的判断；缺评审、评审
   待定或被拒，都不能合并。判定 `changed_meaning` 的字段即使单位一致也不合并。

只有全部成立的边才会被合并成“跨版本可比较指标组”。其余字段仍按版本单独成组，
在数据产品中明确标为 `version_only`，并给出原因。

## 资料与治理文件

输入资料目录需包含四个契约 CSV（格式见 `docs/资料契约.md`）：

| 文件 | 作用 |
| --- | --- |
| `protocols.csv` | 协议版本登记，含生效日期、评审状态、父版本 |
| `schema_fields.csv` | 字段定义（名称、类型、单位、语义引用、是否必填） |
| `run_results.csv` | 每次实验的单字段结果，保留原始文本 |
| `mappings.csv` | 跨版本字段映射的声明与审批状态 |

可选治理文件（通过 CLI 参数或 `load_registry` 传入）：

| 文件 | 谁维护 | 作用 |
| --- | --- | --- |
| `semantic_reviews.csv` | 实验负责人 | 对每条映射判断科学含义：`same_quantity` / `renamed_only` / `changed_meaning` / `split` / `unknown` |
| `unit_conversions.csv` | 数据治理 | 登记经批准的单位换算系数与截距 |
| `mapping_adjudications.csv` | 数据治理 | 对冲突映射记录给出正式裁定，覆盖重复行的矛盾 |

约定：`reviewer_role` 必须为 `experiment_owner`；`approval_state` 与
`review_state` 取值受限；`decided_at` 必须是带时区的 ISO 时间；CSV 均为
UTF-8（可带 BOM）、`\n` 换行。

### 重复行如何处理

合成资料刻意包含重复与冲突，装载器不会“挑一行了事”：

* 完全相同的重复行：去重并记录 `info`。
* 同一字段的**物理定义**（名称/类型/单位）冲突：抛出 `PhysicalSchemaConflict`，拒绝装载。
* 同一字段只有**治理注解**（`semantic_ref` / `required`）冲突：保留全部注解，
  字段标记为 `disputed`，语义引用不再参与合并。
* 同一映射键的多条记录审批状态不同：所有记录保留，按编号最大者视为最新，
  同时输出 `mapping_decision_conflict`；除非有正式裁定，否则该边不可合并。

## 快速开始

```sh
# 运行测试
python3 -m unittest discover -s tests -v

# 只装载资料，查看协议血统与映射决策历史
PYTHONPATH=src python3 -m protocol_schema_evolution_lake.cli lineage --data data

# 生成构建摘要（不落盘）：哪些指标可跨版本合并、哪些必须分开、排除了什么
PYTHONPATH=src python3 -m protocol_schema_evolution_lake.cli build \
    --data data --name lab-shared --include-states approved,draft,retired

# 发布为不可变数据集版本
PYTHONPATH=src python3 -m protocol_schema_evolution_lake.cli publish \
    --data data --name lab-shared --lake ./lake --dataset-version v1 \
    --include-states approved,draft,retired

# 校验文件哈希；按记录追溯
PYTHONPATH=src python3 -m protocol_schema_evolution_lake.cli verify \
    --lake ./lake --name lab-shared --dataset-version v1
PYTHONPATH=src python3 -m protocol_schema_evolution_lake.cli trace \
    --lake ./lake --name lab-shared --dataset-version v1 --run-id RUN-0012
```

安装为包后可用 `protocol-lake` 代替 `PYTHONPATH=src python3 -m protocol_schema_evolution_lake.cli`。

## 兼容数据集的结构

发布目录不可变，结构如下：

```
lake/
  catalog.json                     # 可变账本：发布顺序、替代关系、受影响分区
  datasets/<name>/<version>/
    manifest.json                  # 清单：参数、输入指纹、文件哈希、分组摘要
    lineage.json                   # 协议血统、映射决策历史、逐组变换依据
    registry_snapshot.json         # 本次使用的协议/字段快照（含原始行号）
    governance_snapshot.json       # 映射、评审、换算、裁定的完整历史
    metrics.csv                    # 每个版本字段是否可跨版本比较及原因
    compatibility.csv              # 每条映射的裁决：结论、依据、历史
    exclusions.csv                 # 未进入数据集的记录及结构化原因
    issues.csv                     # 装载与构建过程中的诊断
    partitions/<group_id>.csv      # 指标组分区（按组放宽/收紧比较范围）
```

`partitions/*.csv` 同时保留 `original_value` / `original_unit` 与
`measure_value` / `measure_unit`，以及 `transform`、`semantic_basis`、
`annotation_status`、`value_flags`。分析使用者既能拿到统一口径的度量，
也能回到原始语义。

`metrics.csv` 的 `status` 只有两种取值：

* `cross_version_comparable`：该组内所有成员都有连续、获批准的映射，且语义评审、
  必要换算齐备；
* `version_only`：只能在本版本内使用，`reasons` 列给出具体阻断原因。

## 版本追溯与不可变发布

* **发布即冻结**：目录一旦生成，内容哈希写入 `manifest.json`；同版本、同内容重复发布
  直接复用；同版本、不同内容抛 `ImmutableArtifactError`，绝不静默覆盖。
* **修订只增不改**：协议修订后用新版本重建并显式 `--supersedes 旧版本`。旧版本目录
  保持字节不变，只在 `catalog.json` 中标记 `superseded_by` 与受影响分区
  （`added` / `removed` / `changed`）。已引用旧结果的分析不会被动改变。
* **可重建**：`rebuild` 从已发布清单读回构建参数，用同一资料重放；参数、
  输入哈希与治理指纹都记录在清单里，内容一致时 `content_digest` 相同。
* **可校验**：`verify` 逐文件比对 SHA-256，并报告清单外多余文件。

## 合成资料的含义

仓库自带的 `data/` 是刻意构造的“困难资料”：16 条映射全部与物理定义矛盾
（`same` 的两端单位不同、`split` 只登记了一个目标字段、`unit-convert` 两端字段名不同），
且每个字段的治理注解都有互相冲突的重复行。因此用它构建时，**没有任何指标可跨版本合并**，
每个字段都以 `version_only` 输出并附带原因——这正是应得的保守结论。

`fixtures/coherent/` 与 `fixtures/adjudicated/` 是小型正例，演示：

* 同名同单位的合法合并；
* 经批准的单位换算（g→kg）；
* 仅改名（`renamed_only`）仍可合并；
* 字段被拆成两项时要求重新观测（`split_requires_reobservation`）；
* 实验负责人判定为 `changed_meaning` 时阻断合并；
* 重复映射记录冲突，经 `mapping_adjudications.csv` 正式裁定后恢复可合并。

## Python API

```python
from protocol_schema_evolution_lake import (
    load_registry, resolve_parameters, build_dataset, publish,
    verify_dataset, trace_dataset,
)

registry = load_registry(
    "fixtures/coherent",
    reviews_path="fixtures/coherent/semantic_reviews.csv",
    conversions_path="fixtures/coherent/unit_conversions.csv",
)
build = build_dataset(registry, "coherent", resolve_parameters(registry))
for group in build.groups:
    print(group.group_id, group.status, group.reasons)
result = publish(build, registry, "lake", "v1")
assert verify_dataset("lake", "coherent", "v1") == []
```

主要对象：`Registry`（装载结果与指纹）、`EdgeAssessment`（单条映射裁决）、
`MetricGroup`（可比较指标组）、`DatasetBuild`（未发布构建）、
`publish` 返回 `PublicationResult`。

## 目录

```
data/                             合成资料（四个契约 CSV）
fixtures/coherent/                正例：可合并、需换算、需重观测、语义变更
fixtures/adjudicated/             正例：重复映射冲突经裁定解决
src/protocol_schema_evolution_lake/
  errors.py                       错误类型
  util.py                         确定性 CSV/JSON、哈希、版本与数值工具
  model.py                        领域模型与受控词表
  registry.py                     装载、校验、版本视图、血统
  compatibility.py                兼容性裁决与指标分组
  dataset.py                      构建、发布、校验、追溯
  cli.py                          命令行
tests/                            75 个单元与端到端测试
docs/资料契约.md                  输入字段契约（原始资料说明）
```
