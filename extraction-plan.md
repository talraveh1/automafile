# Implementation plan — section-based text extraction with adaptive caps

## Goal

Stop sending the entire text of long documents to the LLM. Replace today's flat-string extraction + 6000-char post-hoc slice with structured, per-section extraction that:

- caps how much *work* extractors do (don't parse/OCR pages we'll throw away),
- carries page/slide/sheet structure through to the prompt so the LLM knows *where* the text came from ("Page 3 of 47"),
- adapts depth based on content density (read more pages only if the early ones were thin),
- trims at word boundaries so we don't hand the model "in accordance with the parag" mid-word.

No backward-compatibility constraints — change schema and call sites freely.

---

## 1. Schema changes — `automafile/extractors/base.py`

Add a `Section` dataclass and replace `text` semantics:

```python
@dataclass
class Section:
    label: str | None       # "Page 3", "Slide 7", "Sheet: Invoices", or None for non-paginated formats
    text: str               # already trimmed by per_page_chars cap, on a word boundary
    index: int              # 0-based section index in the original document
```

`ExtractedDoc` changes:

- **Add** `sections: list[Section]` (always populated; even non-paginated formats produce `[Section(label=None, text=..., index=0)]`).
- **Add** `total_sections: int | None` — the *original* count (pre-cap) so the prompt can render "Page 3 of 47". `None` for non-paginated formats.
- **Keep** `text: str` as a flat join of `sections[*].text` separated by `\n\n` — convenient for the sidecar body and any caller that doesn't care about structure. Compute it in `__post_init__` rather than asking each extractor to maintain both.
- **Keep** `per_page_chars: list[int] | None` — still needed by `ocr.pdf_ocr_decision`. Document that this is *full* per-page char counts from the text-layer pass, **not** the trimmed/capped values.
- **Remove** nothing else; `format`, `extracted_metadata`, `ocr_*`, `error` stay.

The `has_text` property becomes `bool(self.sections and any(s.text.strip() for s in self.sections))`.

---

## 2. Adaptive cap algorithm — new module `automafile/extractors/_caps.py`

A single helper used by every paginated extractor. Pure function, no I/O:

```python
@dataclass(frozen=True)
class CapConfig:
    min_pages: int = 3
    max_pages: int = 5
    per_page_chars: int = 1500
    target_chars: int = 6000

def select_pages(
    page_texts: Iterable[str],   # generator — extractor yields lazily so we can stop early
    cfg: CapConfig,
) -> list[str]:
    """
    Pull pages from the iterator one at a time. Trim each to `cfg.per_page_chars`
    on a word boundary. Always pull at least `cfg.min_pages`. Stop pulling more
    once accumulated trimmed-char total hits `cfg.target_chars`. Never exceed
    `cfg.max_pages`. Returns the trimmed page texts in order.
    """
```

**Algorithm in detail:**

1. `kept: list[str] = []`
2. For `i, raw in enumerate(page_texts)`:
   - If `i >= cfg.max_pages`: stop iterating (don't even pull the next page from the generator — this is what saves OCR/parse work).
   - `trimmed = trim_to_word_boundary(raw, cfg.per_page_chars)`
   - `kept.append(trimmed)`
   - If `len(kept) >= cfg.min_pages` and `sum(len(p) for p in kept) >= cfg.target_chars`: stop.
3. Return `kept`.

**Word-boundary trim** — separate helper in the same module:

```python
def trim_to_word_boundary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # Look back up to 50 chars for whitespace; if none found, hard-cut.
    last_ws = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
    if last_ws >= max_chars - 50:
        return cut[:last_ws].rstrip()
    return cut.rstrip()
```

The 50-char lookback is to avoid degenerate trims on Chinese/Japanese (no spaces), URLs, or long unbroken tokens — fall through to a hard cut rather than chopping huge content. Hebrew and English will land on a space ~always.

**Iterator contract.** Extractors must yield page text *lazily* — a generator, not a pre-built list. This is what lets `select_pages` stop the underlying parse/OCR work after `max_pages`. For PDF that means the `for page in reader.pages` loop is wrapped in a generator. For multi-page TIFF, the `Image.seek(i)` loop is the generator. For PPTX, slide iteration. Etc.

---

## 3. Per-extractor refactors

### 3a. `extractors/pdf.py`

- Refactor `extract()` so the page loop becomes an inner generator function `_iter_pages(reader)`.
- Call `select_pages(_iter_pages(reader), CapConfig.from_settings(settings))` to get the trimmed kept pages.
- Build `sections = [Section(label=f"Page {i+1}", text=t, index=i) for i, t in enumerate(kept)]`.
- Set `total_sections = len(reader.pages)` (cheap — pypdf doesn't parse text to count).
- `per_page_chars` continues to come from the *full* text-layer pass for OCR-decision purposes — but now we have a problem: if we stop iterating at page 5, we don't have per-page chars for pages 6+.
  - **Resolution:** the OCR decision needs `per_page_chars` for *all* pages to decide which to OCR. Two options:
    1. Run a separate cheap pre-pass that only collects per-page char counts (no full text). pypdf doesn't expose this without doing the text extraction, so it's not actually cheaper.
    2. Move the cap logic to *after* OCR. Extract+OCR everything as today, then cap.
    3. Cap OCR independently: only OCR the pages we've decided to keep.
  - **Recommended: option 3.** Restructure the pipeline so OCR is per-section not per-document. The text-layer pass yields trimmed pages via `select_pages`; for each kept page that's text-empty, OCR *that page*. `per_page_chars` becomes the per-page chars of *kept* pages only. Update `ocr.pdf_ocr_decision` to operate per-page or rename it (`should_ocr_page(text: str) -> bool`).
  - This is a meaningful pipeline change and the agent should flag it before implementing in case it ripples further than expected. If it does, fall back to option 2 (cap after OCR) as a temporary measure and file a follow-up.
- Drop `_per_page_chars` and `per_page_char_counts` if option 3 succeeds (no longer needed by anyone). Verify with `find_referencing_symbols`.

### 3b. `extractors/image.py`

- Add multi-frame support via `Image.n_frames` / `Image.seek(i)`.
- Generator yields OCR text per frame.
- Single-frame images: one section, `label=None`, `total_sections=None` (or `1`? — use `None` to signal "not paginated" semantically; reserve numeric counts for genuinely multi-frame).
- Multi-frame: `label=f"Page {i+1}"`, `total_sections=n_frames`.
- Adaptive caps apply (don't OCR frames past `max_pages`).
- **Vision-model fallback (TBD — see §6) applies per-frame.** A multi-page TIFF can hold scanned photos (e.g. a faxed photo album, a multi-page scan of artwork), so each frame independently goes through the OCR-then-vision-fallback decision.

### 3c. `extractors/pptx.py`

- One slide = one section. `label=f"Slide {i+1}"`, `total_sections=slide_count`.
- Generator iterates `presentation.slides`.
- Per-slide text is the concatenation of shape text. Trim/cap as usual.

### 3d. `extractors/xlsx.py`

- One sheet = one section. `label=f"Sheet: {sheet_name}"`, `total_sections=sheet_count`.
- Caps: `min_pages` / `max_pages` apply to sheets. `per_page_chars` trims each sheet's serialized text. (Sheets can be enormous — this is exactly the format that needed this most.)

### 3e. `extractors/epub.py`

- One spine item = one section. `label=f"Chapter {i+1}"` or the chapter title if available from the TOC.
- `total_sections = spine length`.

### 3f. `extractors/docx.py`

- DOCX has soft pages (Word reflows them) and python-docx doesn't expose page boundaries.
- **Decision: one section, no label, `total_sections=None`.** Treat as non-paginated. The `per_page_chars` cap doesn't apply; only `target_chars` does (treat the whole doc as one "page" and trim the single section to `target_chars` on a word boundary).
- Future improvement (out of scope): split on H1 headings as pseudo-sections. Note in code comment, don't implement.

### 3g. `extractors/html.py`, `extractors/text.py`

- Single section, `label=None`, `total_sections=None`.
- Trim to `target_chars` on word boundary.

### 3h. `extractors/unknown.py`

- No change beyond schema migration (sections list with one empty/error section).

---

## 4. Prompt rendering — `automafile/llm.py`

`_build_prompt` (currently at line 111) and `enrich` (line 311) change:

- Take `ExtractedDoc` (or specifically `sections` + `total_sections`) instead of a flat `text: str`. Update `enrich`'s signature accordingly and update callers (`pipeline.py`).
- New helper `_render_sections(sections, total_sections) -> str`:
  ```
  --- Page 1 of 47 ---
  <section text>

  --- Page 2 of 47 ---
  <section text>
  ```
  - If `total_sections is None` and there's exactly one unlabeled section, render the bare text (no separator).
  - If `total_sections is not None` but only some pages are present (because we stopped at `max_pages`), append a trailing line: `--- (showing pages 1-5 of 47) ---` so the LLM knows it didn't see the whole doc.
- **Drop the `[:6000]` slice in `_build_prompt`** — caps are enforced upstream now. Keep `sanitize_excerpt` (still relevant for Hebrew quote handling).
- Update `automafile/prompts/triage.txt` to mention that `{text}` may be a partial document with page labels, so the LLM treats unseen content as "unknown" rather than absent.

---

## 5. Config — `automafile/config.py`

Add a section:

```jsonc
"extraction": {
  "min_pages": 3,
  "max_pages": 5,
  "per_page_chars": 1500,
  "target_chars": 6000
}
```

with matching defaults in the Settings dataclass. Plumb through to extractors via `CapConfig.from_settings(settings)`. Update `config.example.jsonc`.

---

## 6. Vision-model fallback — **TBD, do not implement in this pass**

Sketch only, leave a `# TODO(vision):` comment in `extractors/image.py` at the per-frame decision point:

- Per frame: run OCR; if OCR result is below a length/confidence threshold (e.g. <50 chars of meaningful text), fall back to a vision-capable Ollama model.
- Vision call: send the frame image + a short prompt like *"Describe this image in 2-3 sentences for filing purposes — what is it (photo / chart / screenshot / form / diagram), what is the subject, is there any visible text or identifying detail?"*
- Use the description as the section's `text`.
- Gated by a config flag `vision_model: str | null` (null disables, default null).
- Applies per-frame in multi-page TIFFs (scanned photo collections).
- Open questions to resolve before implementing: which Ollama vision model (`llava`, `llama3.2-vision`, `minicpm-v`?), does our `_ollama_generate` need to grow image-input support, latency budget, what does "OCR failed" mean precisely (char-count threshold? Tesseract confidence?).

This is its own follow-up task. Do not block this PR on it.

---

## 7. Tests

Add to `tests/`:

- `test_caps.py` — unit tests for `select_pages` and `trim_to_word_boundary`:
  - min_pages floor: 3 pages always returned even when target hit on page 1
  - max_pages ceiling: 5 pages max even when target never hit
  - target stops expansion: target hit on page 4 → no page 5 pulled (verify via mock generator that page 5 was never requested)
  - per_page trim: each page trimmed to per_page_chars
  - word boundary: trim lands on whitespace when whitespace exists within 50-char lookback
  - hard cut: trim falls through to char boundary on Chinese/long-token input
  - empty pages: counted toward min_pages but not toward target
- `test_pdf_extractor.py` — extend existing tests:
  - 100-page PDF with text on every page → only 5 sections returned, `total_sections=100`
  - 2-page PDF → 2 sections, `total_sections=2`
  - Multi-page PDF with thin pages (50 chars each) → 5 pages returned (target never hit, max wins)
  - Multi-page PDF with one fat first page (10000 chars) → first page trimmed to 1500, still pulls min_pages=3
- `test_image_extractor.py` — multi-page TIFF fixture, verify section iteration and caps apply.
- `test_xlsx_extractor.py` — multi-sheet workbook, verify each sheet becomes a section with `Sheet: <name>` label.
- `test_pptx_extractor.py` — multi-slide deck.
- `test_llm_prompt.py` — verify `_render_sections` formats correctly with/without labels, with/without trailing "showing pages X-Y" line.

For PDF tests that need multi-page fixtures, generate with reportlab in a fixture or check in a small `.pdf` to `tests/fixtures/`.

---

## 8. Migration / cleanup

- Delete `_per_page_chars` and `per_page_char_counts` from `pdf.py` if §3a option 3 succeeds. Run `find_referencing_symbols` first.
- Delete the `[:6000]` slice in `_build_prompt`.
- Update `metadata/sidecar.py` if it stores `text` differently — verify it just uses the flat join (which is unchanged in spirit, just regenerated from sections).
- Check `pipeline.py` for any place that re-trims or reuses raw `text` and update.

---

## 9. Order of work

1. Add `Section` dataclass + `sections`/`total_sections` to `ExtractedDoc`. Land schema first so the rest can branch off it.
2. Implement `_caps.py` with tests.
3. Refactor `pdf.py` (the headline case). Resolve the OCR-per-page question (§3a) — flag if it's bigger than expected.
4. Update `llm.py` and `prompts/triage.txt`.
5. Update `pipeline.py` and any other callers.
6. Roll out to `image.py` (with multi-frame), `pptx.py`, `xlsx.py`, `epub.py`, `docx.py`, `html.py`, `text.py`, `unknown.py`. Each is a small, testable change.
7. Add config plumbing (§5).
8. Run the full test suite + a manual smoke test on a few representative real documents (a fat PDF, a multi-sheet XLSX, a multi-page TIFF).

---

## 10. Out of scope (explicitly)

- Vision-model fallback (§6 — TBD, separate task).
- Splitting DOCX on headings.
- Token-aware (vs char-aware) budgeting.
- Head+tail sampling. Front-only is the deliberate choice for filing.
- Changing the sidecar format.
- Any change to OCR engine selection or Ollama model selection.

---

Hand this whole plan to the implementing agent verbatim. The two design judgement calls they'll need to reconfirm before coding: (a) the OCR-per-page restructuring in §3a, (b) the DOCX "one section, no pagination" decision in §3f.
