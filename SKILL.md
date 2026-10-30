---
name: studio-imagegallery-publish
description: 使用可配置的 API 适配器，将本地 imagesgallery 包发布到 Studio，创建为 imagegallery block，不依赖 fira-llms/fcs 运行时代码。
---

# Studio ImageGallery 推送

使用这个 skill，把本地 `imagesgallery` 产物（图片 + 音频 + 时间轴字幕）发布到 Studio。

## 真实推送的凭据前置检查

真实推送创建或编辑 block 之前，必须先完成认证。`--dry-run` 不访问 Studio 或 OSS，也不会登录；不需要认证输入，请求计划中的认证字段会显示为 `<redacted>`。

优先使用的认证输入：

1. 环境变量（固定变量，推荐）：
   - `FIRA_SAAS_OP_ACCOUNT`
   - `FIRA_SAAS_OP_PASSWORD`
2. CLI 参数：
   - `--account`
   - `--password`
3. 现有的 Studio Cookie：
   - `--cookie`

如果执行真实推送时以上方式都没有提供，这个 skill 必须停下，并明确提醒用户配置：

- `FIRA_SAAS_OP_ACCOUNT`
- `FIRA_SAAS_OP_PASSWORD`

安全边界：

- 不要要求用户提供阿里云 AK/SK。
- 不要在 Codex 与用户的交互流程里要求 AK/SK。
- 音频上传所需的 STS 数据，应来自 Studio `get_upload_info` 的返回结果。

## 这个 Skill 会做什么

- 将 Studio URL 解析为 `studio_base` 和 `vertical_block_id`
- 读取本地 `imagesgallery.json`，并校验所需文件是否存在
- 在目标 vertical 下创建一个新的 `imagesgallery` block
- 按页面顺序通过 `handler/file_upload` 上传页面图片
- 在调用 `handler/get_upload_info` 之后，走 OSS multipart 流程上传完整 MP3
- 通过 `documents` API 注册音频并绑定到 block
- 保存逐页字幕 / 时间轴配置

这个 skill 是自包含的，不会导入 `fira-llms` 或 `fira-gpt-fcs` 的运行时代码。

## 必需输入

- 一个 Studio 小节 URL（`/container/block-v1:...`）或显式的 `vertical_block_id`
- `imagesgallery.json`（Stage-B schema，包含 `audio` 和 `items[].page/start/end/subtitle`）
- 可选的 `--adapter` JSON（通常不需要；默认模板已适配大多数站点）

## 工作流

1. 仅在真实推送前运行凭据检查；dry-run 无需凭据
2. 根据 URL + manifest 构建运行时上下文
3. 先做 dry-run 请求渲染（推荐）
4. 执行真实推送：
   - 创建 block
   - 上传图片
   - 上传 / 注册 / 绑定音频
   - 保存播放配置和展示名称
   - 如提供课程结构信息，先核对来源 `block_type`，再按类型替换正文并校验 vertical；独立的 `本节要点组件` 会保留
5. 返回目标 `vertical` 的可点击 Studio URL（运营复核入口）、创建出的 `block_locator`、新建的 `imagesgallery` block URL，以及推送摘要

## 就地替换与发布完整性

- `block_type` 是每个 target 的来源契约。发布器核对 Gallery manifest、publish target 和可用的 `section-list.json`；缺失或冲突时，在 Studio 写入前停止该 target。不要依据 Studio 现有子组件推断来源类型。
- `html` target 只在目标 vertical 中替换第一个直接 HTML 子组件 `文字讲解组件`。没有匹配项时停止该 target。`imagesgallery` target 只在目标 vertical 中替换唯一的 Gallery 替换目标组件；目标缺失或有多个候选时停止该 target。
- 在第一次 Studio 写入前保存目标 vertical 的纵向组件快照，记录直接子组件数量、locator、类型和顺序。创建 Gallery、移动 Gallery、删除替换目标组件后分别重新读取 Studio，并执行状态对账。数量相同但 locator、类型或顺序不同也属于失败。
- 创建、移动或删除收到成功响应后也要核对纵向组件快照。若响应不确定或状态未达到预期，先重新读取 Studio；目标状态已经生效时，按 `completed` 或 `reconciled` 记录并继续，能确认未生效时最多重试一次，无法确认时停止。保留最初的纵向组件快照作为整个恢复过程的基线。
- 只有所有结构步骤都与预期纵向组件快照相符，才发布 vertical；Studio 必须明确返回 `published=true` 才算小节正式发布状态通过。未解决的创建、移动、删除或结构问题会跳过该 vertical 的最终发布，并记录失败步骤与预期、观察快照。
- 发布前检查 Gallery 页和 speech 输入中的独立 `本节要点组件` 内容。发现泄漏时，在创建 Gallery 前将该 target 标为失败并跳过 vertical 发布；Studio 原有的 `本节要点组件` 继续保留。
- 结果报告记录前后快照、步骤状态、重试记录、发布状态和可验证的 skill / 输入 provenance。无法确认的 provenance 写作 `unknown`。单 Gallery operation 文件中的 `vertical_integrity` 证据供批次验证器核对。

## 本节要点组件保留

- 普通发布、批量发布、失败重试和中断恢复都会按原流程替换小节正文，同时保留独立的 `本节要点组件`。
- `本节要点组件` 的位置、数量、标题和可见内容不会触发删除，也不会阻止正文替换。发布器会将识别到的组件从替换目标候选中排除。
- 旧的 `--cleanup-only` 参数仍可识别，但会在读取 Studio 课程结构、认证或发出请求前返回 `unsupported`（退出码 `2`）。
- 批量 targets 中标记 `cleanup_only` 的目标返回 `unsupported`；批次继续处理其他目标，在汇总中统计该目标，并以退出码 `2` 返回。

## 批量推送安全规则

如果这个 skill 是在更大的批量工作流里被调用：

- 上游的本地准备工作可以并行，但真正的 Studio 推送阶段必须 **顺序执行**
- **不要**在同一个运营会话里并发向 Studio 推送多个 deck
- 原因：并发的 Studio 登录 / 会话刷新会互相打架，容易造成间歇性认证失败，例如 `401`、`Not Login yet`、csrf/session 失效，或者部分音频注册失败
- 安全模式应当是：
  - 如有需要，可以并行准备本地 `imagesgallery` 产物
  - 先把 deck A 推到 Studio
  - deck A 完整成功后，再推 deck B
  - 然后再推 deck C，以此类推

## 认证错误重试规则

在 execute 模式下，如果某个推送步骤因认证 / 会话相关错误失败，脚本应当：

- 将 `401`、`Not Login yet`、csrf/session 失效，以及类似登录/认证错误，视为可重试的认证失败
- 刷新 Studio 登录 / 会话
- 每次重试前等待几秒钟
- 最多重试 **4** 次
- 这个重试策略只适用于认证相关失败，不适用于普通业务 / 数据错误

典型可重试场景包括：

- `upload_audio_register` 返回 `401 Not Login yet`
- 后续保存步骤因为 csrf/session 在运行中途过期而失败

如果认证相关重试次数耗尽，必须停下，并清楚报告失败发生在哪个步骤。

## 命令接口

```bash
python scripts/publish_imagegallery_block.py \
  --studio-url "https://studio.uat.firacademy.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@XXXX" \
  --manifest "/path/to/imagesgallery/imagesgallery.json" \
  --dry-run
```

```bash
python scripts/publish_imagegallery_block.py \
  --studio-url "https://studio.uat.firacademy.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@XXXX" \
  --manifest "/path/to/imagesgallery/imagesgallery.json" \
  --execute \
  --courses-base "https://courses.uat.firacademy.com"
```

```bash
python scripts/publish_imagegallery_block.py \
  --studio-url "https://studio.uat.firacademy.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@XXXX" \
  --manifest "/path/to/imagesgallery/imagesgallery.json" \
  --course-structure-json "/path/to/fira_course-....json" \
  --source-vertical-block-id "block-v1:ORG+COURSE+RUN+type@vertical+block@XXXX" \
  --execute
```

```bash
python scripts/publish_imagegallery_block.py \
  --targets-file "/path/to/imagegallery-push-studio-targets.json" \
  --report-file "/path/to/imagegallery-publish-report.json" \
  --execute
```

参数说明：

- `--studio-url`：目标 Studio 容器 URL
- `--manifest`：本地 `imagesgallery.json` 路径
- `--adapter`：可选适配器配置路径（默认使用 `references/adapter.template.json`）
- `--execute`：执行真实请求
- `--dry-run`：离线打印渲染后的请求计划，不登录、不向 Studio 或 OSS 发请求；认证字段使用 `<redacted>` 占位符，也不要求认证输入
- `--account` / `--password`：可选账号密码覆盖（默认取环境变量）
- `--cookie`：可选的 Cookie 认证模式
- `--csrf-token`：可选的 CSRF 覆盖值
- `--skip-oss-multipart`：调试模式；拿到上传信息后跳过 OSS 上传
- `--auth-retry-max-retries`：认证 / 会话错误的最大重试次数；默认 `4`
- `--auth-retry-delay-seconds`：认证重试之间的等待秒数；默认 `3`
- `--course-structure-json`：课程结构快照，用于就地替换小节正文
- `--source-vertical-block-id`：当目标课是导入副本时，用源课 vertical locator 做课程结构匹配
- `--section-id` / `--section-title` / `--block-type` / `--section-list-json`：target 类型和 section-list 来源元数据
- `--cleanup-only`：兼容旧命令行调用；返回 `unsupported`，不访问 Studio
- `--targets-file`：批量执行 `ppt-to-imagesgallery` 产出的 Studio targets 文件
- `--report-file`：批量执行的逐 vertical 结果文件；重复执行时用于安全 rerun 判断，并在最终汇总里区分 `replaced`、`fallback_inserted`、`unsupported` 和失败项

每个 vertical 的结果会包含可点击的目标 vertical URL、新建或复用的 gallery URL、`本节要点组件` 保留状态、纵向组件快照、已完成和失败步骤，以及失败原因或人工跟进说明。

## 单 Gallery 发布检查点

批量调用方可在真实单 Gallery 发布时显式传入 `--operation-file` 和 `--gallery-key`。调用方先创建 operation 文件；发布器只更新它，不创建第二份报告。

```bash
python scripts/publish_imagegallery_block.py \
  --studio-url "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@XXXX" \
  --manifest "/path/to/imagesgallery/imagesgallery.json" \
  --execute \
  --operation-file "/path/to/batch-operation.json" \
  --gallery-key "stable-gallery-key"
```

operation 文件包含 `galleries[]`。发布器要求恰有一个 `gallery_key` 与参数相同的条目，并校验其中的 `manifest` 和 `vertical_locator`（如果提供）；未传这两个参数时，单 Gallery 命令保持原有行为，不写 operation 文件。发布器保留条目中的其他调用方字段，并原子更新 `gallery_locator` 和 `publish`：

- `status`、`completed_stage`：状态为 `in_progress`、`failed` 或 `completed`；阶段依次为 `block_created`、`images_uploaded`、`images_ordered`、`audio_prepared`、`audio_uploaded`、`audio_registered`、`audio_binding_saved`、`audio_bound`、`saved`。
- `images`、`asset_ids`：已确认的页码、图片文件名和 Studio asset ID。
- `steps`、`active_step`、`failed_step`、`error_summary`：安全的步骤状态、HTTP 状态和脱敏错误摘要。

每个远程步骤开始前和确认完成后都会原子更新检查点。图片上传没有返回可用 ID 时，发布器停止后续上传和排序，只对 `get_files` 做一次恢复读取；仅当文件总数、名称顺序和唯一 asset ID 与 manifest 完全匹配时才继续。状态不完整或有歧义时，保留现场并停止，不盲目重传。恢复时若图片与排序已确认，发布器从音频阶段继续。响应正文、Cookie、CSRF 值、Authorization、STS 凭据和 token 不进入输出或 operation 文件。

## 批量发布后的验证与修复

外部批量工作流负责创建 `batch-operation.json`，按顺序发布所有目标 Gallery 和 vertical，并在初始发布全部结束后调用一次验证器。它继续负责本地素材准备；验证器只读取这些 manifest 与 Studio 状态。

每个 `galleries[]` 条目至少包含 `gallery_key`、`manifest`、`vertical_locator`、`gallery_locator`、Ticket 01 的 `publish` 检查点，以及 `initial_vertical_publish: {"status":"completed"}`。验证器只检查 `publish.status == "completed"` 且 `publish.completed_stage == "saved"` 的 Gallery；初始 vertical 发布缺失或失败时仍会采集 Studio 诊断，但批次结果保持未通过。发布器保留调用方字段。验证器原子更新同一文件中的 `validation`、`repair_attempts`、`repair_history` 和 `batch_result`；修复后的 vertical 发布结果记录在对应的 `repair_history[].vertical_publish` 中。

所有初始 vertical 发布完成后执行：

```bash
python scripts/validate_imagegallery_batch.py \
  --studio-url "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@XXXX" \
  --operation-file "/path/to/batch-operation.json" \
  --execute
```

真实执行使用现有 Studio 认证输入：`--account` / `--password`、环境变量 `FIRA_SAAS_OP_ACCOUNT` / `FIRA_SAAS_OP_PASSWORD`，或 `--cookie`。验证器在正常路径只读取一次 Studio `format=concise` 课程结构，再通过 Gallery preview 的 `get_files`、`get_play_config` 和 `get_audios` 检查作者态内容。图片只比较有效文件记录数、`total_count` 和 manifest 页数；配置条目数须等于 Studio 图片数，音频绑定数须为 1。它不比较图片名、顺序或内容，也不检查 learner-side LMS 或远端音频播放。

有效计数不符会记为 `mismatch`，最多触发一次完整修复；请求失败或响应结构无效会记为 `unverified`，不修改该 Gallery，并继续检查其他目标。修复先移除目标 Gallery 的图片引用，再扫描课程中所有 Gallery 的当前引用和完整图片资产清单。仅对确认无引用的原资产执行精确删除；每次删除前都会重新扫描全部 Gallery 引用。随后用本地 package 恢复图片、音频绑定和播放配置，发布所属 vertical，并重新验证。资产扫描或删除前引用检查不完整时跳过对应删除。修复后仍不匹配时停止，不开始第二次完整修复。

若进程在修复记录完成最终校验前中断，可用同一 operation 文件重跑验证器：Gallery 状态仍可读时续跑现有修复尝试并复用各阶段检查点；已确认完成的步骤不会重复执行，完整修复次数仍为一次。读取状态为 `unverified` 时不继续修改，待读取恢复后再重跑。

`all_sections_verified` 只有在每个目标最终为 `verified` 且初始 vertical 发布成功（修复过的 Gallery 还须修复后 vertical 发布成功）时才为 true。退出码 `0` 表示全部通过，`1` 表示至少一个目标未通过，`2` 表示 operation、认证或参数无法执行。输出和 operation 文件仅保留安全字段、计数、asset ID、HTTP 状态和脱敏错误摘要，不保存 Studio 响应正文或凭据。`--skip-oss-multipart` 仅用于调试，会跳过修复发布中的音频对象上传。

## Adapter 概念
Adapter 用来定义请求模板。对大多数 FIRAcademy 站点，一个通用 adapter 就够用了，因为 `studio_base` / `courses_base` 和 block locator 都会在运行时解析出来：

- `create_block`
- `upload_image`
- `reorder_children`
- `delete_block`
- `publish_vertical`
- `upload_audio`
- `save_block`

每个模板支持：

- `method`、`url`
- `headers`、`params`、`json`、`data`
- `files`（multipart 文件上传）
- `extract`（把响应中的值提取到运行时上下文，例如 `block_locator`）

## 资源

- `scripts/publish_imagegallery_block.py`：端到端发布脚本
- `scripts/validate_imagegallery_batch.py`：批量发布后的 Studio 校验与单次安全修复
- `references/adapter.template.json`：默认通用 adapter

## 说明

- 自定义 adapter 时，请保持 adapter 里的 URL 为完整 URL
- 第一次接入新站点时，务必先跑 `--dry-run`
- 如果本机缺少 `oss2`，脚本会自动安装
- block category 固定为 `imagesgallery`
- execute 成功后，必须向用户**优先返回目标 vertical URL**，因为运营复核和细调通常要回到小节页面；不能只给原始 `block_locator`
- 如果需要补充技术细节，可以附带新建的 `imagesgallery` block URL，但它不应作为默认主链接
- 在批量完成总结里，要把每一个目标 `vertical` URL 分开列出，方便运营逐个点进去做人工微调
- 批量 rerun 时，脚本会在每个目标开始前重新读取 `course_structure_json`，避免整轮都沿用同一份旧快照
