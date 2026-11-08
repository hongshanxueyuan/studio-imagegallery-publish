# 自审与修复规则

读取本页时机：首次批次发布全部尝试结束后、解释自审状态、决定是否能自动修复或恢复未完成修复时。

## 自审核对范围

批次 validator 读取 operation 文件、本地 manifest 和 Studio 实时状态。它只核对本批次 Gallery 及其目标 vertical，不扫描或修改批次外小节。存在可用 Gallery 和 vertical locator 时，即使初次发布步骤失败也读取当前状态。写请求成功本身不作为结果证据。

Studio 课程结构必须是可读取的完整树；课程、chapter、sequential 或 vertical 任一层缺少可读子节点时，相关结构标记为无法确认。每个目标逐项核对：

- 图片有效记录数量、manifest 页数、逐图名称、顺序和本次发布记录中的 asset ID。
- Studio 已保存的字幕配置可读取，时间轴条目数与本地页面数一致；每条有唯一正整数页码、非负整数开始时间、不早于开始时间的整数结束时间和非空字幕。若响应包含 `show_subtitles`，其值必须为 `true`；字段缺省时不据此判失败。不比较字幕文字和时间戳是否与本地 manifest 相同。
- Studio 音频接口恰有一条 MP3 绑定；不检查学员端播放或音质。
- vertical 的两个直接子组件顺序：首位 Gallery，末位“本节要点” HTML；替换目标已移除。
- Studio 当前保存的小节已正式发布且没有待发布改动。仅凭发布请求成功不能通过；Studio 返回值不足以确认当前状态时标为无法确认。

Gallery 读取、operation 证据或课程结构无效、不可读或不足以判断时，标记为无法确认。继续独立目标和检查；不根据无法确认结果发起课程写入。
Studio 返回有效的零图片列表而本地 manifest 有页面时，即使首次发布没有 asset ID，也确认图片不一致并允许安全修复。

## 安全修复

一轮审核覆盖整个 batch。只修复结果已确认、目标唯一且结构足以安全定位的问题；将本轮所有可修复目标依次处理后，重新审核整个 batch。最多执行两轮“审核→修复→审核”，通过后提前结束。

图片修复前先移除目标 Gallery 的图片引用，再扫描课程中全部 Gallery 的当前引用与图片资产清单。只删除已确认没有课程引用的原图片；每次删除前重新扫描所有 Gallery 引用。扫描不完整或引用有歧义时，跳过对应删除。用本地 package 恢复图片、音频绑定和播放配置，并发布所属 vertical。

修复 checkpoint、失败步骤或异常读取导致进程中断时，使用原 operation 文件恢复。已确认完成的步骤不可重复；同一批次修复轮次上限仍为两轮。每轮成功或失败后都重新检查全部 target。修复响应成功仍不作为通过证据，最终以 Studio 当前保存状态为准。

## 操作记录

每个 Gallery 的 `publish` 和 `initial_vertical_publish` 保留首次发布执行结果；`validation` 保存审核轮次、预期、实际观察值、检查状态和安全错误摘要；`repair_publish`、`repair_attempts`、`repair_history` 保存修复 checkpoint、次数、步骤与发布结果。`course_structure_verification` 保存目标小节结构与当前发布状态。`batch_result.all_sections_verified` 只描述审核结果，`operator_report.text` 分别汇总首次发布、审核和修复。

运行入口：

```bash
python scripts/validate_imagegallery_batch.py \
  --studio-url "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@XXXX" \
  --operation-file "/path/to/batch-operation.json" \
  --execute
```

退出码 `0` 表示全部目标通过，`1` 表示至少一个目标有问题或无法确认，`2` 表示 operation、认证或参数不足以执行。`--skip-oss-multipart` 只用于调试修复流程，不上传音频对象。
