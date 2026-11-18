# 自审与修复规则

读取本页时机：首次批次发布全部尝试结束后、解释自审状态、决定是否能自动修复或恢复未完成修复时。

## 自审核对范围

批次 validator 读取 operation 文件、本地 manifest 和 Studio 实时状态。老课程包可用 `--targets-file` 启动；若显式提供 operation 文件，必须先确认其课程与 `--studio-url` 中的课程一致。未提供 operation 文件时，验证器查找目标文件同目录下的 `<目标文件名>.batch-operation.json`，有则复用，无则按课程、小节 ID 和唯一标题映射目标。小节或 Gallery 映射有歧义时停止该目标并报告，继续其他独立目标。validator 只核对本地包列出的目标 Gallery 和 vertical，不扫描或修改包外小节。存在可用 Gallery 和 vertical locator 时，即使初次发布步骤失败也读取当前状态。写请求成功本身不作为结果证据。

Studio 课程结构必须是可读取的完整树；课程、chapter、sequential 或 vertical 任一层缺少可读子节点时，相关结构标记为无法确认。每个目标逐项核对：

- 图片有效记录数须等于本地页数，文件名及顺序须与 manifest 相同。operation 文件提供了本次发布的有序 asset ID 时，也核对 Studio 当前引用；没有可用 ID 记录的老包按文件名和顺序核验。
- Studio 已保存的字幕时间轴须包含与本地页面对应的唯一正整数页码、非负整数开始时间、不早于开始时间的整数结束时间和非空字幕，并逐页精确匹配本地 manifest 的字幕文本和开始、结束时间。若响应包含 `show_subtitles`，布尔值 `true` 或 `false` 都是学员端显示设置，不据此判断字幕缺失；字段缺省时也不据此判失败。
- Studio 音频接口恰有一条绑定，并通过该绑定的音频 ID 请求 `get_audio_download_url`。对返回的 GET 签名地址发送 `Range: bytes=0-0`：200/206 且读到字节为“对象可访问”；404/`NoSuchKey` 为确定缺失；其他 HTTP 状态、网络或签名错误、空响应均为无法确认。不要对该 GET 地址发 HEAD，也不要用 CORS OPTIONS 判断对象存在。
- 本地 manifest 和其中引用的图片、音频及字幕时间轴素材均可读取。仅当本地包完整且目标唯一时，才能比较 Studio 内容或写入修复。
- vertical 的两个直接子组件顺序：首位 Gallery，末位“本节要点” HTML；替换目标已移除。
- Studio 当前保存的小节已正式发布且没有待发布改动。仅凭发布请求成功不能通过；Studio 返回值不足以确认当前状态时标为无法确认。

图片、字幕或音频读取无效，operation 证据或课程结构无效、不可读或不足以判断时，对应检查标记为无法确认。对象长度只作为诊断信息，不与本地大小比较，也不据此声称 MP3 完整或字节相同。继续独立目标和检查；不根据无法确认的组件状态修复。课程结构或 Gallery 归属无法确认时不发起 Studio 写入。
Studio 返回有效的零图片列表而本地 manifest 有页面时，即使首次发布没有 asset ID，也确认图片不一致并允许安全修复。

## 安全修复

一轮审核覆盖整个 batch。只修复已确认不一致、本地 Gallery 完整、目标唯一且结构足以安全定位的目标；同一 Gallery 中其他无法确认的检查仍保持无法确认。Gallery 内容确认不一致时，从本地包整体替换 Gallery，不把新图片、字幕和音频与旧 Gallery 混合。若 Studio 中目标小节没有 Gallery，则只在唯一匹配的小节内创建。将本轮所有可修复目标依次处理后，重新审核整个 batch。

- 整段替换时只移除旧 Gallery 组件及其引用；不删除 Studio 课程图片资产，也不扫描全课程资产清单。
- 音频绑定缺失、多条绑定或对象确定缺失（404/`NoSuchKey`）时，可按本地包整体替换 Gallery。对象访问无法确认时不据此修复。
- 替换时写入本地完整图片、字幕 `time_config` 和音频绑定；保留可读取的布尔 `show_subtitles` 设置，`false` 仍有效。
- 对目标 Gallery 完成替换后发布所属 vertical，再从 Studio 重新读取 Gallery 内容、对象可访问性和小节发布状态。

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
