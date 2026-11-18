# 本地素材恢复规则

读取本页时机：用户明确要求修复，且目标本地 Gallery 缺少图片、音频或字幕时间轴，或 manifest 中的本地素材路径失效时。普通发布请求不触发本地重建；本地 Stage-B 包完整时也不重跑生成器。

## 目标和范围

先将 Studio vertical 唯一映射到一个本地 manifest。只有需要重建图片时，才进一步唯一确定源 PPT 和对应的 `section-list.json` 记录；若缺少页面讲解文本，再唯一确定可提供文本的 section 记录。优先用 `section_id` 匹配；没有 ID 时才用唯一的 section 标题匹配。无法唯一确定目标或所需来源时，不猜路径、不启动整批规划，也不发布该目标；将该目标报告为无法验证，并继续其他独立目标。

只对受影响小节调用 `ppt-to-imagesgallery` skill 的 `scripts/repair_imagesgallery_materials.py`。不要运行 `plan_batch_jobs.py`，也不要直接用会重建整个 Gallery 目录的单 deck builder 覆盖现有 Stage-B 包。

```bash
python <ppt-to-imagesgallery-skill-root>/scripts/repair_imagesgallery_materials.py \
  --manifest "/path/to/imagesgallery.json"
```

`--manifest` 必需。图片缺失时必须同时提供 `--ppt` 与 `--section-json`，两者须精确对应同一目标小节；只缺字幕或时间轴且 manifest 的页面讲解文本不完整时，可单独提供 `--section-json` 补全文本。若音频需重建但 manifest 已有完整页面文本，则无需 `--section-json`。`--audio-search-root` 可选；修复失效的音频路径时，将它设为范围尽可能窄的本地课程素材目录。省略时，工具只在 Gallery 包目录内搜索。

## 按缺失阶段恢复

- **图片缺失**：在隔离的暂存目录中重渲染目标小节的图片阶段，再把生成的页图和 manifest 图片引用合并回现有包；保留现有 Stage-B 音频、字幕时间轴和其他字段。不要让 builder 清理或覆盖原 Gallery 目录。
- **音频路径失效但文件可能存在**：按原文件名在限定搜索目录内查找 MP3。唯一匹配时只更新 `audio.full_audio` 路径；若字幕时间轴完整，不运行 TTS。多个匹配时不猜测，停止该目标的恢复并报告无法验证。没有匹配时按缺少完整 MP3 处理，继续判断是否能用现有分段音频恢复或需要重新生成。
- **音频、字幕或时间轴本身缺失**：只对当前 manifest 运行生成器的音频阶段，并复用已存在的分段音频（`--skip-existing`）。页面讲解文本不完整时提供唯一匹配的 `--section-json`；既没有可复用音频也没有讲解文本来源时，停止该目标的恢复并报告无法验证。用户已明确要求修复时，不再为该次必需的 TTS 单独询问费用；缺少认证或生成失败时，停止该目标的发布并报告无法验证。

恢复工具只处理传入的单个 manifest，不扫描或重建其他小节。运行成功后确认该 manifest 已恢复为完整 Stage-B 包，再将同一 manifest 交给发布器。不要绕过发布器的认证和写入安全规则。

## 发布和最终状态

使用 [发布和替换规则](publishing-and-replacement.md) 的原位发布入口及 operation 文件，再把同一 operation 文件交给[自审与修复规则](self-audit-and-repair.md)中的 validator。保留 ticket 01 的 Studio 修复与复审流程；本地重建成功本身不能当作 Studio 已通过的证据。

逐目标报告本地恢复及 Studio 复核结果：Studio 当前状态全部核实且符合预期才报成功；读取到确认差异时报不一致；来源映射、生成结果或 Studio 证据不足时报无法验证。素材无法恢复的目标不继续写入，独立目标仍按批次流程处理。
