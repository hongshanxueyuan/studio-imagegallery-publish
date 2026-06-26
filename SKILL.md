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
5. Return created `block_locator` and publish summary.

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
