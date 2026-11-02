# 发布和替换规则

读取本页时机：真实推送、来源组件替换、发布失败恢复或 operation checkpoint 异常时。

## 认证和敏感信息

真实 Studio 写入前通过凭据检查。认证输入优先级：

1. 环境变量 `FIRA_SAAS_OP_ACCOUNT` 和 `FIRA_SAAS_OP_PASSWORD`。
2. CLI 参数 `--account` 和 `--password`。
3. 现有 Studio Cookie：`--cookie`。

批量发布优先使用环境变量，便于 publisher 和 validator 使用同一会话凭据。批次 validator 还支持 `FIRA_SAAS_OP_COOKIE` 和 `FIRA_SAAS_OP_CSRF_TOKEN`。不要索要或传递阿里云 AK/SK；音频上传的 STS 信息从 Studio `get_upload_info` 响应取得。

dry-run 不访问 Studio 或 OSS、不登录，也不要求认证。真实执行缺少凭据或认证失败时，将受影响目标记录为失败或无法确认，跳过依赖操作并继续独立目标。

## 来源组件和小节结构

发布器使用显式 `block_type` 契约识别替换目标，不从 Studio 现有组件猜测来源：

- `html` target 替换目标 vertical 下第一个直接 HTML “文字讲解组件”。
- `imagesgallery` target 替换唯一的 Gallery 替换目标。
- 缺少来源类型、来源冲突、目标缺失或目标不唯一时，在 Studio 写入前停止该目标。

首次 Studio 写入前保存 vertical 子组件快照。替换或删除前重新读取 Studio，核对 locator、类型、顺序和数量。最终 vertical 只有两个直接子组件：首位是本批次 ImageGallery，末位是独立的“本节要点” HTML；旧替换目标已移除。

“本节要点”独立 HTML 不属于替换目标。普通发布、失败重跑和中断恢复都保留它。课程包中的总结内容仍按当前发布契约处理。

## 批次顺序和请求恢复

对 Studio 的写入必须按 target 顺序执行。不要并发发布多个 target，以免登录会话和认证状态互相干扰。独立 target 失败后记录失败原因，继续后续 target。

请求状态、asset ID、步骤和安全错误摘要写入 operation 文件。响应正文、Cookie、CSRF、Authorization、STS 凭据和 token 不写入输出或记录。

单 Gallery checkpoint 可通过 `--operation-file` 与 `--gallery-key` 启用。调用方创建 operation 文件；发布器只更新匹配的 gallery 条目中的 `gallery_locator` 和 `publish`。逐步记录上传状态、页面及 asset ID、失败步骤和脱敏诊断。图片上传没有可用 ID 时，才进行一次 `get_files` 对账；只有数量、名称、顺序和唯一 asset ID 与 manifest 完全匹配才继续。创建 Gallery 的响应不确定时，用创建前快照尝试唯一恢复 locator；不能唯一确认时保留现场，避免重复创建。

认证相关请求失败可刷新 Studio session 并重试，最多 4 次。该策略只适用于认证失败；其他业务错误不重试。

## CLI 输入

单 target dry-run：

```bash
python scripts/publish_imagegallery_block.py \
  --studio-url "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@XXXX" \
  --manifest "/path/to/imagesgallery.json" \
  --dry-run
```

真实批次：

```bash
python scripts/publish_imagegallery_block.py \
  --targets-file "/path/to/imagegallery-push-studio-targets.json" \
  --report-file "/path/to/imagegallery-publish-report.json" \
  --execute
```

`--report-file` 保存每个 vertical 的发布结果，用于安全 rerun 判断。课程 structure 输入可通过 `--course-structure-json` 提供；导入副本可用 `--source-vertical-block-id` 指定源课程 vertical。`--cleanup-only` 兼容旧调用但返回 `unsupported`，不访问 Studio。

target 来源元数据可通过 `--section-id`、`--section-title`、`--block-type` 和 `--section-list-json` 指定。发布结果应优先提供目标 vertical URL，方便运营回到小节；可额外提供新建的 `imagesgallery` block URL。

发布前后都保留独立的 `本节要点组件`。只有目标来源契约指定的正文组件会被替换。

单 Gallery batch checkpoint 的完整参数由 `python scripts/publish_imagegallery_block.py --help` 展示。

## Adapter

默认 adapter 位于 `references/adapter.template.json`。新接入站点前先运行 dry-run。Adapter 用请求模板描述 Studio/OSS 接口；脚本在运行时解析 studio_base、courses_base 与 block locator。本页不重复 adapter 字段清单，以配置文件和 CLI 帮助为准。
