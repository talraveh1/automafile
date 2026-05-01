# Architecture

```mermaid
flowchart LR
    inbox(["📥 Inbox dir"]):::source
    watcher["<b>watcher.py</b><br/><i>watchdog PollingObserver</i>"]:::watch
    pipeline(["<b>pipeline.py</b>"]):::core

    subgraph extract ["Extraction"]
        direction TB
        dispatch["<b>dispatch.py</b><br/><i>ext / MIME</i>"]:::stage
        extractors["<b>extractors/*</b><br/>pdf · docx<br/>xlsx · pptx<br/>image · html<br/>epub · text"]:::stage
        ocr["<b>ocr.py</b><br/><i>run_ocr — tesseract</i>"]:::stage
        ocr_decision["<b>pdf_ocr_decision</b><br/><i>per-page chars</i>"]:::stage
    end

    llm["<b>llm.py</b> · Ollama<br/><i>tiered JSON parse</i>"]:::ai
    writers["<b>metadata writers</b><br/>native.py │ sidecar"]:::write
    notifier["<b>notifier.py</b><br/><i>Windows toast (optional)</i>"]:::notify

    cli["<b>cli.py</b><br/><i>filer-apply · scan · review-ocr · reconcile</i>"]:::tool
    triage[/"🗂️ /triage skill"/]:::tool

    inbox --> watcher --> pipeline
    pipeline --> dispatch --> extractors --> llm
    pipeline --> ocr --> ocr_decision --> llm
    llm --> writers --> notifier
    triage <-.-> cli

    classDef source fill:#e3f2fd,stroke:#1976d2,stroke-width:2px,color:#0d47a1
    classDef watch  fill:#fff3e0,stroke:#f57c00,stroke-width:2px,color:#e65100
    classDef core   fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px,color:#4a148c
    classDef stage  fill:#fafafa,stroke:#616161,stroke-width:1.5px,color:#212121
    classDef ai     fill:#e8f5e9,stroke:#388e3c,stroke-width:2px,color:#1b5e20
    classDef write  fill:#fce4ec,stroke:#c2185b,stroke-width:2px,color:#880e4f
    classDef notify fill:#ede7f6,stroke:#5e35b1,stroke-width:1.5px,color:#311b92
    classDef tool   fill:#fffde7,stroke:#f9a825,stroke-width:1.5px,color:#f57f17
```

## Data flow

1. A file lands in `<documents_root>/<inbox_dir>`.
2. `watcher` debounces creation/move events for `watch_settle_seconds`,
   verifies size stability, then dispatches to `pipeline.process_file`.
3. `pipeline.process_file` calls `dispatch.extract` to choose an extractor by
   suffix (or MIME, via `python-magic`).
4. The extractor returns an `ExtractedDoc` containing text, native metadata
   (if any), and an OCR recommendation.
5. If OCR is recommended, `ocr.run_ocr` is invoked (Tesseract for images;
   `pdf2image` + Tesseract for PDFs).
6. `llm.enrich` calls Ollama. The response is parsed via tiered fallbacks:
   strict → repair → retry → regex → placeholder.
7. The metadata is written via `metadata.native.write` (with mtime
   preserved) when the format supports it; otherwise via
   `metadata.sidecar.write`. Native-bearing files **also** get a sidecar so
   the in-tree memory is consistent.
8. A debounced toast announces the change.

## Scanner

`automafile scan` walks the tree, builds a hash index (cached by
`(mtime, size)` in `storage/scan/hash-index.json`), and emits a worklist
JSON to `storage/scan/scan-<ts>.json`. It identifies:

- `files_needing_ocr` — text-layer-less PDFs, images without metadata.
- `files_needing_metadata` — supported types with no sidecar/native data.
- `files_with_partial_metadata` — sidecars missing required fields.
- `files_with_stale_metadata` — file mtime newer than `metadata_modified`.
- `ocr_review_candidates` — files OCR'd with a different engine/lang.
- `orphan_sidecars` — sidecars whose target file is missing, with hash
  matches in the tree.
- `unprocessable_files` — encrypted PDFs, etc.

## Memory

Project-local memory lives in [memory/](memory/). The `/triage` skill
reads `preferences.md`, `taxonomy.md`, and `corrections.jsonl` on every
invocation and updates them when the user overrides a proposal.

## mtime preservation

Every native-metadata writer wraps its work in `metadata.mtime.preserve_times`,
which captures `(atime_ns, mtime_ns)` before writing and restores via
`os.utime(..., ns=...)` after. The file's content hash will change (OneDrive
re-syncs the new bytes); Explorer's "Date modified" stays at the original.
