# 运营报告规则

读取本页时机：向运营说明一批 ImageGallery 发布的最终结果，或检查报告是否与 operation 记录一致时。

使用 `batch_result.operator_report.text` 作为运营说明。开头分别汇总首次发布、后置审核及修复；每条问题放在对应目标小节下，并给出可直接打开的 Studio vertical 链接。

默认按目标小节组织。需要按问题类型查看时，可运行 validator 并传入 `--report-mode categories`；两种视图都只列出本批次的待处理或无法确认结果。

- 通过项、成功请求、内部步骤状态不逐项展示。
- 不在运营说明中写字段名、asset ID、HTTP 状态、请求日志、原始响应、Cookie、CSRF、token 或其他凭据。
- 证据不足时说明哪节的哪项课程结果无法核实，并用普通语言说明已知原因。
- 全部通过时保留简短阶段汇总，不生成成功步骤清单。

详细 HTTP 状态、操作步骤、locator、asset ID、重试与修复历史、安全诊断保存在独立的 `batch-operation.json`。排查失败时先按报告中的目标链接打开 Studio，再结合对应的 operation 条目查看证据。运营说明与结构化记录需要描述同一最终状态；请求响应本身不覆盖 Studio 实时核对结果。
