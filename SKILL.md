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
   - 如提供课程结构信息，则按课程结构执行就地替换 / 删除旧块 / 发布 vertical
5. 返回目标 `vertical` 的可点击 Studio URL（运营复核入口）、创建出的 `block_locator`、新建的 `imagesgallery` block URL，以及推送摘要

## HTML 来源课程的重复要点清理

- 修改来源元数据的生成或传递时，也检查 `ppt-to-imagesgallery` 的[批任务字段传递](https://github.com/hongshanxueyuan/ppt-to-imagesgallery/blob/main/scripts/plan_batch_jobs.py)、[Stage-A manifest 生成](https://github.com/hongshanxueyuan/ppt-to-imagesgallery/blob/main/scripts/build_imagesgallery.py)和[Stage-B 字段保留](https://github.com/hongshanxueyuan/ppt-to-imagesgallery/blob/main/scripts/synthesize_imagesgallery_audio.py)。这三处共同决定 `section-list.json` 中的来源信息如何到达发布器。
- 新生成的 `imagesgallery.json` 应携带顶层 `block_type`、`section_id` 和 `section_title`。
- 发布器优先读取 manifest 的 `block_type`。旧 manifest 缺少该字段时，使用 `section-list.json`：先按 `section_id` 精确匹配；只有 ID 不可用时才允许按唯一小节标题匹配。
- 来源标记缺失、冲突或匹配不唯一时，正常发布仍可替换正文，但不会自动删除 HTML；结果会标记人工跟进并在可识别时附上目标 block 链接。
- 仅当来源为 HTML、图库最后一页标题是“本节要点”，且目标 vertical 末尾有唯一匹配的 HTML 组件时，正常发布才会额外删除该组件。图库完成保存后才删除正文和旧摘要；任一删除失败都会停止发布，不会回滚已完成的 Studio 操作。
- 已完成课程可使用 cleanup-only。先运行 dry-run 检查 vertical、图库末页摘要证据和唯一 HTML 删除目标，再使用相同参数运行 `--execute`。cleanup-only 不创建或上传新 gallery：

```bash
python scripts/publish_imagegallery_block.py \
  --studio-url "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@XXXX" \
  --course-structure-json "/path/to/course-structure.json" \
  --cleanup-only \
  --dry-run
```

```bash
python scripts/publish_imagegallery_block.py \
  --studio-url "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@XXXX" \
  --course-structure-json "/path/to/course-structure.json" \
  --cleanup-only \
  --execute
```

执行前会重新读取 Studio 课程结构，并要求同一 vertical 中存在末页标题为“本节要点”的 gallery 和唯一末尾 HTML 摘要组件。

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
- `--course-structure-json`：课程结构快照；用于就地替换，也为 cleanup-only dry-run 提供预览数据
- `--source-vertical-block-id`：当目标课是导入副本时，用源课 vertical locator 做课程结构匹配
- `--section-id`：manifest 没有来源标记时，用于回查 `section-list.json` 的小节 ID
- `--section-title`：仅当小节 ID 不可用时，用于唯一标题回退
- `--block-type`：来源 block 类型元数据；与 manifest 或 `section-list.json` 冲突时会禁止自动摘要清理
- `--section-list-json`：显式指定来源元数据回查文件；未指定时只检查 manifest 同目录中的 `section-list.json`。如果文件位于其他目录，请显式传入路径，避免误用其他课程的来源信息。
- `--cleanup-only`：只预览或删除已验证 gallery 对应的末尾 HTML“本节要点”，不上传新 gallery
- `--targets-file`：批量执行 `ppt-to-imagesgallery` 产出的 Studio targets 文件
- `--report-file`：批量执行的逐 vertical 结果文件；重复执行时用于安全 rerun 判断，并在最终汇总里区分 `replaced`、`fallback_inserted`、`cleaned`、`manual_followup_failed`

每个 vertical 的结果会包含可点击的目标 vertical URL、旧 HTML 摘要 block 链接与组件名（如已定位）、新建或复用的 gallery URL、摘要清理状态、已完成和失败步骤，以及失败原因或人工跟进说明。

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
