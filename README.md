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
iterations. Each iteration answers a specific way in which the model failed to stay faithful.

## From one sentence to a robust prompt system

| Iteration | Failure it answers | What changed |
|---|---|---|
| v0 | n/a | A single natural-language instruction |
| v1 | Creative drift; photo effects baked into the artwork | A fidelity lock: ranked sources of truth and hard constraints, in a dual-register prompt |
| v1.1 | Seller-poster clutter leaking into the design | An explicit model of what a poster adds around a garment |
| v2 | Panels that don't belong to one garment | Decomposition into print blocks, conditioned on an anchor panel |
| v3 | Unseen parts invented as confidently as seen ones | An evidence grade for each part before drawing; the wording adapts to the evidence |
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

**Anchor-conditioned decomposition.** A garment is produced as independent print blocks, one model call each. Later
blocks are conditioned on finished earlier ones, but only where coherence is wanted. Anchoring everywhere turned out
to be a failure mode of its own. Scale travels between calls in inches, never pixels.

**Evidence-adaptive wording.** A reasoning model grades how much of each part the reference really shows. The image
model is never asked to judge evidence; it receives wording already chosen for the evidence it has.

**Prompts as code.** Prompts are compiled deterministically from templates, so identical inputs give byte-identical
prompts. Every change is regression-checked against the full set of rendered prompt variants, and every call is
recorded with its exact prompt, inputs, usage and cost.

## Principles distilled

1. **Rank your sources, or the model will rank them for you.**
2. **Describe size in physical units.** They survive resolution changes; pixel instructions don't.
3. **Condition where you want coherence; withhold where you want independence.**
4. **Ship the acceptance test inside the prompt.**
5. **State every hard constraint twice.**
6. **Keep data and instructions in separate, declared channels.**
7. **Let a reasoning model choose the wording;** let the image model draw.
8. **Treat prompts as compiled artefacts:** deterministic, diffable, regression-tested.

## Beyond the prompt

- **Print geometry:** outputs land on the exact print block at 300 dpi (7500 × 10200 px for a body panel), with
  memory-bounded upscaling. A 76 MP panel needs 444 MB instead of 922 MB, small enough for a 1 GiB VM.
- **Colour-layer vectorization:** one compound path per colour, on named layers in SVG, EPS and PDF. On the test
  harness: no gaps or colour spill, about a quarter of the anchor points of a standard colour tracer, and corners
  within about 0.8 px.
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
browser (ES modules, no build step, bilingual UI)
   ▼
FastAPI ─ session auth with role-scoped demo access ─ rate limiting
   ▼
JobManager (async jobs, retries, per-call ledger, background preparation of downloads)
   ├─ prompts    template compiler (pure, deterministic)
   ├─ providers  GPT Image · Claude / OpenAI text · offline mock
   ├─ imaging    upscaler · palette · layer tracer · SVG / EPS / PDF writers
   └─ storage    one folder per job, with an append-only call ledger
```

## Quick start

```bash
git clone https://github.com/yaad96/pattern-seer-ai
cd pattern-seer-ai
./run.sh          # serves http://127.0.0.1:8000
```

API keys are read only from key files in the project root (`.openai_api_key`, and optionally `.anthropic_api_key`).
Without them, the app runs an offline mock. Settings are described in `.env.example`.

The prompt templates and the descriptor vocabulary in this repository are **structural placeholders**: the same
sections, variables and data channels, in generic wording. The production prompt set is maintained privately and
replaces them at runtime when present.

## Limitations and future work

- **The evaluation is engineering-level.** A paired benchmark of client references against the factory's final files
  comes next.
- **Paired parts share one design,** so a part that differs between left and right loses its unique details.
- **Complexity-routed inference (planned):** cheaper open-weight editors for simple designs and the frontier model
  for complex ones, with a prompt dialect for short-context text encoders.

## Stack

Python 3.11+ · FastAPI · Pydantic · Jinja2 · Pillow · NumPy · vtracer · OpenAI SDK · Anthropic SDK · vanilla ES
modules.
