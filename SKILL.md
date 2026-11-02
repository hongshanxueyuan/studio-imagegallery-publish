---
name: studio-imagegallery-publish
description: 使用可配置的 API 适配器，将本地 imagesgallery 包发布到 Studio，并核实本批次的最终课程状态。
---

# Studio ImageGallery 推送

本 skill 负责从本地素材发布到 Studio，并完成目标小节的自审、必要修复和最终报告。用户明确启动真实批次后，按已有输入连续完成流程，不在中途询问处理选择。输入缺失、认证失败或安全前置条件未通过时，将受影响写入步骤记为失败并跳过依赖操作；Studio 读取失败、响应无效或证据不足时，将检查结果记为无法确认且不据此写入。两类情况都继续处理独立目标并完成批次报告。

真实 Studio 写入前先通过认证检查。优先使用环境变量中的 Studio 运营账号密码，也可使用命令行账号密码或现有 Cookie。dry-run 不登录、不访问 Studio 或 OSS，也不需要认证。认证与敏感信息规则见 [发布和替换规则](references/publishing-and-replacement.md)。

## 必需输入

- 一个 Studio 小节 URL，或显式 vertical locator。
- 符合 Stage-B schema 的 ImageGallery manifest，包含页面、字幕时间轴和完整 MP3。
- 批次发布可选用目标文件、报告文件和课程结构信息。完整输入约定见 [发布和替换规则](references/publishing-and-replacement.md)。

## 工作流

### 1. 发布

按目标顺序推送每个 Gallery，使用来源 block_type 找到并替换对应正文组件，保留“本节要点”HTML，并发布小节。首次发布尝试全部结束后，operation 文件应记录每个目标的 locator 和安全操作证据。写入、重跑和恢复规则见 [发布和替换规则](references/publishing-and-replacement.md)。

### 2. 自审

调用批次验证器，读取 Studio 当前保存状态，只核对本批次目标。确认图片、字幕、MP3、最终小节组件结构和发布状态。自审标准、证据不足时的处理和安全边界见 [自审与修复规则](references/self-audit-and-repair.md)。

### 3. 修复与复审

只对已确认且可安全重做的问题执行修复。每轮集中处理本批次全部可修复目标，然后重新审核整批；最多两轮，全部通过时提前结束。Studio 读取失败或证据不足时不写入。修复前先读取 [自审与修复规则](references/self-audit-and-repair.md)。

## 完成与报告

所有目标都应有最终审核状态并写入 operation 文件。全部通过时，`batch_result.all_sections_verified` 为 `true`。否则从 `batch_result.operator_report.text` 取面向运营的说明；通过项和成功请求不逐条展示。报告规则见 [运营报告规则](references/operator-report.md)。详细步骤、资源 ID、HTTP 状态、修复历史和脱敏诊断留在独立的 `batch-operation.json` 记录中。

验证器命令：

```bash
python scripts/validate_imagegallery_batch.py \
  --studio-url "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@XXXX" \
  --operation-file "/path/to/batch-operation.json" \
  --execute
```

## 资源

- `scripts/publish_imagegallery_block.py`：单 Gallery 和批次发布
- `scripts/validate_imagegallery_batch.py`：批次自审与安全修复
- `references/adapter.template.json`：默认 API adapter
