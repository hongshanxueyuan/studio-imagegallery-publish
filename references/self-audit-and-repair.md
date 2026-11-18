# 自审与修复规则

读取本页时机：首次批次发布全部尝试结束后、解释自审状态、决定是否能自动修复或恢复未完成修复时。

## 自审核对范围

批次 validator 读取 operation 文件、本地 manifest 和 Studio 实时状态。它只核对本批次 Gallery 及其目标 vertical，不扫描或修改批次外小节。存在可用 Gallery 和 vertical locator 时，即使初次发布步骤失败也读取当前状态。写请求成功本身不作为结果证据。

Studio 课程结构必须是可读取的完整树；课程、chapter、sequential 或 vertical 任一层缺少可读子节点时，相关结构标记为无法确认。每个目标逐项核对：

- 图片只按有效记录数与本地 manifest 页数对账；名称、顺序和 asset ID 只作为观察信息，不决定通过或修复。
- Studio 已保存的字幕时间轴条目数与本地页面数一致；每条有唯一正整数页码、非负整数开始时间、不早于开始时间的整数结束时间和非空字幕。若响应包含 `show_subtitles`，布尔值 `true` 或 `false` 都是学员端显示设置，不据此判断字幕缺失；字段缺省时也不据此判失败。不比较字幕文字和时间戳是否与本地 manifest 相同。
- Studio 音频接口恰有一条绑定；不检查学员端播放或音质。
- vertical 的两个直接子组件顺序：首位 Gallery，末位“本节要点” HTML；替换目标已移除。
- Studio 当前保存的小节已正式发布且没有待发布改动。仅凭发布请求成功不能通过；Studio 返回值不足以确认当前状态时标为无法确认。

图片或音频读取无效、operation 证据或课程结构无效、不可读或不足以判断时，对应检查标记为无法确认。继续独立目标和检查；不根据无法确认的图片或音频状态修复该组件。若字幕配置读取失败，则按下方规则仍推送完整本地时间轴，但读取失败持续存在时最终状态必须保持无法确认。课程结构或 Gallery 归属无法确认时不发起课程写入。
Studio 返回有效的零图片列表而本地 manifest 有页面时，即使首次发布没有 asset ID，也确认图片不一致并允许安全修复。

## 安全修复

一轮审核覆盖整个 batch。只修复已确认不一致、目标唯一且结构足以安全定位的组件；同一 Gallery 中其他无法确认的组件仍保持无法确认。将本轮所有可修复目标依次处理后，重新审核整个 batch。

- 图片数量不一致时，移除目标 Gallery 当前引用的旧图片，再上传完整本地图片集并调整顺序。只移除 Gallery 引用；不删除 Studio 课程图片资产，也不扫描全课程资产清单。
- Gallery 音频绑定数量不等于一条时，仅修复音频绑定。
- 字幕时间轴缺失或条目数、格式不符合要求时，仅写入完整本地 `time_config`。保留可读取的布尔 `show_subtitles` 设置；该设置为 `false` 时仍视为有效。若字幕配置无法读取，也写入完整本地 `time_config`；写后仍无法读取则最终标记为无法确认。
- 对目标组件完成修复后发布所属 vertical，再从 Studio 重新读取当前状态复核。

每个新的显式修复请求都重新读取当前 Studio 状态，并最多执行两轮“审核→修复→审核”，通过后提前结束。每轮修复无论成功或失败，都重新审核整个 batch。修复 checkpoint、失败步骤或异常读取导致进程中断时，使用原 operation 文件恢复当前未完成请求；已确认完成的步骤不可重复，恢复执行沿用该请求剩余轮次。后续新的显式请求重新取得两轮上限。修复响应成功仍不作为通过证据，最终以 Studio 当前保存状态为准。

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
