# Pattern Seer AI

**Fidelity-locked prompting for garment design reconstruction.**
This is an applied prompt-engineering study. It turns a frontier image model from a generator into a reproduction
instrument: from a low-resolution jersey photo to print-ready, layered-vector production panels.

---

## Abstract

Image models are trained to *create*. A sublimation factory needs the opposite. The input is a client's
low-resolution photo or a seller's mockup poster. The output must be the same design, cleaned of everything that
isn't the garment, and split into the physical pieces the factory prints, each at its real size.

A one-line instruction does not survive that task. Pattern Seer AI is the prompt system that does, refined over eight
iterations. Each iteration answers a specific way in which the model failed to stay faithful. This repository starts
from the finished system, with placeholder prompts (see Access), so its history doesn't show the iterations.

## From one sentence to a robust prompt system

| Iteration | Failure it answers | What changed |
|---|---|---|
| v0 | n/a | A single natural-language instruction |
| v1 | Creative drift; photo effects baked into the artwork | A fidelity lock: ranked sources of truth and hard constraints, in a dual-register prompt |
| v1.1 | Seller-poster clutter leaking into the design | An explicit model of what a poster adds around a garment |
| v2 | Panels that don't belong to one garment | Decomposition into print blocks, conditioned on an anchor panel |
| v3 | Unseen parts invented as confidently as seen ones | An optional evidence grade for each part, made while the front is drawn; the sleeve's wording adapts to the evidence |
| v4 | Paired parts that disagree | One shared design for paired parts |
| v5 | Wrong geometry; garment details drawn into print blocks | Canvas = print block; scale expressed in physical units; hard rules stated twice |
| v6 | Over-conditioning: one panel copying another | Selective anchoring |
| v7 | Clients who want only one part | A reference for each part; the anchor moves to whichever panel exists |

Client-supplied text is a separate, declared data channel throughout, so it can describe a design but never rewrite
the rules.

## Core ideas

**Fidelity lock.** The system prompt doesn't ask for quality; it ranks authority. Where the rules, the task, the
reference image and staff notes disagree, the order decides, and the model's own taste ranks last.

**Dual-register prompts.** Each prompt speaks twice: in binding prose, and in a dense descriptor written in the
model's own idiom. Constraints that matter appear in both. The prompt also carries its own acceptance test, which the
model applies before it finishes.

**Anchor-conditioned decomposition.** A garment is produced as independent print blocks, one model call each. A back
with no picture of its own needs no call: it is the front, mirrored or copied. Later blocks are conditioned on
finished earlier ones, but only where coherence is wanted. Anchoring everywhere turned out to be a failure mode of
its own. Scale travels between calls in inches, never pixels.

**Evidence-adaptive wording.** An optional pre-pass has a reasoning model grade how much of each part the reference
really shows. The image model is never asked to judge evidence. The front is drawn while the grade is made, so only
the sleeve prompt receives wording already chosen for the evidence it has. The front keeps the generic wording; its
grade becomes a label for staff.

**Prompts as code.** Prompts are compiled deterministically from templates, so identical inputs give byte-identical
prompts. Every call is recorded with its exact prompt, a fingerprint of each input image, usage and cost.

## Principles distilled

1. **Rank your sources, or the model will rank them for you.**
2. **Describe size in physical units.** They survive resolution changes; pixel instructions don't.
3. **Condition where you want coherence; withhold where you want independence.**
4. **Ship the acceptance test inside the prompt.**
5. **State every hard constraint twice.**
6. **Keep data and instructions in separate, declared channels.**
7. **Let a reasoning model choose the wording;** let the image model draw.
8. **Treat prompts as compiled artefacts:** deterministic, diffable, logged with every call.

## Beyond the prompt

- **Print geometry:** outputs land on the exact print block at 300 dpi (7500 × 10200 px for a body panel), with
  memory-bounded upscaling. In my measurement, the whole process peaks at 444 MB instead of 922 MB for a 76 MP panel,
  small enough for a 1 GiB VM that makes one at a time.
- **Colour-layer vectorization:** one compound path per colour in SVG, EPS and PDF, on named layers in the SVG and
  PDF. On my vector benchmark (not in this repository): no gaps or colour spill, 24–43% of the anchor points of a
  standard colour tracer depending on the preset, and corners within about 0.8 px.
- **Factory sheet:** all panels composed onto one print page, using spacing measured from a real production file.

## Selected measurements

| Metric | Value |
|---|---|
| One 2K production panel (GPT Image 2.5) | 7.4–8.4 ¢ |
| Panel set, including the evidence pre-pass | about 18–19 ¢ |
| Image latency (2K) | median about 34 s |
| Evidence pre-pass | median about 9 s |

## Architecture

```text
browser (ES modules, no build step, Bangla UI)
   ▼
FastAPI ─ session auth with role-scoped demo access ─ rate limiting
   ▼
JobManager (async jobs, retries, per-call ledger, background preparation of downloads)
   ├─ prompts    template compiler (pure, deterministic)
   ├─ providers  GPT Image · Claude / OpenAI text · offline mock
   ├─ imaging    upscaler · palette · layer tracer · SVG / EPS / PDF writers
   └─ storage    one folder per job, with an append-only call ledger
```

## Access

The prompt engineering behind Pattern Seer AI is proprietary. The prompt templates and the descriptor vocabulary in
this repository are structural placeholders: the application runs with them, but not with the production prompt set.
If you're interested in discussing the exact prompts, or in running the full system on your own machine, please get
in touch through my GitHub profile.

## Stack

Python 3.12+ · FastAPI · Pydantic · Jinja2 · Pillow · NumPy · vtracer · OpenAI SDK · Anthropic SDK · vanilla ES
modules.
