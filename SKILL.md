---
name: studio-imagegallery-publish
description: 使用可配置的 API 适配器，将本地 imagesgallery 包发布到 Studio，创建为 imagegallery block，不依赖 fira-llms/fcs 运行时代码。
---

# Studio ImageGallery 推送

使用这个 skill，把本地 `imagesgallery` 产物（图片 + 音频 + 时间轴字幕）发布到 Studio。

## 凭据前置检查（执行前必须完成）

这个 skill 在创建和编辑 block 之前，必须先完成认证。

优先使用的认证输入：

1. 环境变量（固定变量，推荐）：
   - `FIRA_SAAS_OP_ACCOUNT`
   - `FIRA_SAAS_OP_PASSWORD`
2. CLI 参数：
   - `--account`
   - `--password`
3. 现有的 Studio Cookie：
   - `--cookie`

如果以上方式都没有提供，这个 skill 必须停下，并明确提醒用户配置：

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

1. 运行凭据前置检查
2. 根据 URL + manifest 构建运行时上下文
3. 先做 dry-run 请求渲染（推荐）
4. 执行真实推送：
   - 创建 block
   - 上传图片
   - 上传 / 注册 / 绑定音频
   - 保存播放配置和展示名称
5. 返回目标 `vertical` 的可点击 Studio URL（运营复核入口）、创建出的 `block_locator`、新建的 `imagesgallery` block URL，以及推送摘要

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

参数说明：

- `--studio-url`：目标 Studio 容器 URL
- `--manifest`：本地 `imagesgallery.json` 路径
- `--adapter`：可选适配器配置路径（默认使用 `references/adapter.template.json`）
- `--execute`：执行真实请求
- `--dry-run`：只打印渲染后的请求计划，不发送会改状态的调用
- `--account` / `--password`：可选账号密码覆盖（默认取环境变量）
- `--cookie`：可选的 Cookie 认证模式
- `--csrf-token`：可选的 CSRF 覆盖值
- `--skip-oss-multipart`：调试模式；拿到上传信息后跳过 OSS 上传
- `--auth-retry-max-retries`：认证 / 会话错误的最大重试次数；默认 `4`
- `--auth-retry-delay-seconds`：认证重试之间的等待秒数；默认 `3`

## Adapter 概念

Adapter 用来定义请求模板。对大多数 FIRAcademy 站点，一个通用 adapter 就够用了，因为 `studio_base` / `courses_base` 和 block locator 都会在运行时解析出来：

- `create_block`
- `upload_image`
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
