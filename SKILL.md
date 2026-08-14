---
name: studio-imagegallery-publish
description: Publish a local imagesgallery package to Studio as an imagegallery block using configurable API adapters, without relying on fira-llms/fcs runtime code.
---

# Studio ImageGallery Publish

Use this skill to publish local `imagesgallery` output (images + audio + timeline subtitles) into Studio.

## Credential Preflight (Required Before Execute)

This skill must authenticate before creating and editing blocks.

Preferred auth inputs:

1. Environment variables (fixed vars, recommended):
   - `FIRA_SAAS_OP_ACCOUNT`
   - `FIRA_SAAS_OP_PASSWORD`
2. CLI flags:
   - `--account`
   - `--password`
3. Existing Studio cookie:
   - `--cookie`

If none of the above is provided, the skill must stop and explicitly remind the user to configure:

- `FIRA_SAAS_OP_ACCOUNT`
- `FIRA_SAAS_OP_PASSWORD`

Security boundary:

- Do not ask user to provide Alibaba Cloud AK/SK.
- Do not require AK/SK in Codex user input flow.
- Audio upload uses STS data returned by Studio `get_upload_info`.

## What This Skill Does

- Parse Studio URL into `studio_base` and `vertical_block_id`.
- Read local `imagesgallery.json` and verify required files exist.
- Create a new `imagesgallery` block under the target vertical.
- Upload page images in page order via `handler/file_upload`.
- Upload full MP3 via OSS multipart flow after `handler/get_upload_info`.
- Register audio in `documents` API and bind it to block.
- Save per-page subtitle/timeline config.

This skill is self-contained and does not import `fira-llms` or `fira-gpt-fcs` runtime logic.

## Required Inputs

- A Studio section URL (`/container/block-v1:...`) or explicit `vertical_block_id`.
- `imagesgallery.json` (stage-B schema with `audio` and `items[].page/start/end/subtitle`).
- Optional `--adapter` JSON (normally not needed; default template works across sites).

## Workflow

1. Run credential preflight.
2. Build runtime context from URL + manifest.
3. Dry-run request rendering (recommended first).
4. Execute real publish:
   - create block
   - upload images
   - upload/register/bind audio
   - save play config and display name
5. Return created `block_locator`, the final clickable Studio block URL, and publish summary.

## Batch Publish Safety

If this skill is used from a larger batch workflow:

- Local preparation may be parallelized upstream, but the actual Studio publish phase must run **sequentially**.
- Do **not** publish multiple decks to Studio concurrently with the same operator session.
- Reason: concurrent Studio login/session refresh can fight with each other and cause intermittent auth failures such as `401`, `Not Login yet`, csrf/session invalidation, or partial audio-registration failures.
- Safe pattern:
  - prepare local `imagesgallery` outputs in parallel if needed
  - publish deck A to Studio
  - after deck A fully succeeds, publish deck B
  - then publish deck C, and so on

## Auth Error Retry Rule

During execute mode, if a publish step fails with an authentication/session-related error, the script should:

- treat errors like `401`, `Not Login yet`, csrf/session invalidation, and similar login/auth messages as retryable auth failures
- refresh Studio login/session
- wait a few seconds between retries
- retry at most **4** times
- only apply this retry policy to auth-related failures, not generic business/data errors

Typical retryable cases include:

- `upload_audio_register` returns `401 Not Login yet`
- a later save step fails because csrf/session expired mid-run

If the auth-related retries are exhausted, stop and report the failing step clearly.

## Command Interface

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

Arguments:

- `--studio-url`: target Studio container URL.
- `--manifest`: local `imagesgallery.json` path.
- `--adapter`: optional adapter config path (default uses `references/adapter.template.json`).
- `--execute`: execute real requests.
- `--dry-run`: print rendered request plan without sending state-changing calls.
- `--account` / `--password`: optional override credentials (defaults to env vars).
- `--cookie`: optional cookie auth mode.
- `--csrf-token`: optional CSRF override.
- `--skip-oss-multipart`: debug mode; skip OSS upload after receiving upload info.
- `--auth-retry-max-retries`: max retries for auth/session related failures; default `4`.
- `--auth-retry-delay-seconds`: delay between auth retries; default `3`.

## Adapter Concept

Adapter defines request templates, but one generic adapter should cover most FIRAcademy sites because `studio_base`/`courses_base` and block locator are resolved at runtime:

- `create_block`
- `upload_image`
- `upload_audio`
- `save_block`

Each template supports:

- `method`, `url`
- `headers`, `params`, `json`, `data`
- `files` (multipart file upload)
- `extract` (extract response values into runtime context, e.g. `block_locator`)

## Resources

- `scripts/publish_imagegallery_block.py`: end-to-end publisher.
- `references/adapter.template.json`: default generic adapter.

## Notes

- Keep adapter URLs as full URLs when customizing adapter.
- Always run `--dry-run` first for a new site.
- Script auto-installs `oss2` if missing.
- Block category is `imagesgallery`.
- After execute succeeds, always surface the final clickable Studio block URL to the user, not only the raw `block_locator`.
- In batch completion summaries, list every created Studio block URL separately so operators can click in and do manual fine-tuning.
