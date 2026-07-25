# 04 — Token and cost estimation

Tell the user what a run will cost before they start it.

**Depends on:** 01, 03

## Files

- `src/promptheus/estimate.py`
- `tests/test_estimate.py`

## Scope

```python
@dataclass(frozen=True)
class ModelEstimate:
    model_id: str
    input_tokens: int
    input_cost: Decimal          # exact
    max_output_tokens: int
    max_output_cost: Decimal     # upper bound
    exceeds_context: bool

@dataclass(frozen=True)
class RunEstimate:
    per_model: list[ModelEstimate]
    input_cost: Decimal
    max_output_cost: Decimal
    approximate: bool = True
```

- `count_tokens(text) -> int` — `tiktoken` with `o200k_base`.
- `estimate_run(prompt, attachments, models, max_output_tokens)` → `RunEstimate`.

## Design notes

**Input and output are reported separately.** We know roughly what goes in; we
cannot know how much a model will generate. Report them as separate numbers —
`input: $0.042 · output: up to $0.180 at 4k tokens` — rather than one made-up
total. A single number here would be confidently wrong every time.

> **Correction, found while verifying plan 06 against the live API.** This plan
> originally claimed the input cost was *exact*. It is not. We know the text we
> send, but not how each provider tokenises it, and the chat template wraps
> every message in role markers we never count. An 11-token prompt came back
> billed as 15, 16 and 27 prompt tokens on three different models, so the total
> can be exceeded on very short prompts. `MESSAGE_TOKEN_OVERHEAD` now allows
> for the template and the docstrings no longer claim exactness. The *output*
> ceiling did hold exactly, as designed.

**One tokenizer for every model, and say so.** There is no per-model
tokenization endpoint on OpenRouter, and the catalog spans many tokenizer
families (`architecture.tokenizer`). `o200k_base` lands within roughly 10–20%
for most, which is enough to decide whether to press the button. `approximate`
is always `True`; the UI must label the figure as an estimate. Do not pretend
to a precision we do not have.

**All arithmetic in `Decimal`.** Prices are per token:
`input_cost = pricing.prompt * input_tokens`. Quantize only for display, never
mid-calculation.

**Flag context overflow.** If input tokens exceed a model's `context_length`,
set `exceeds_context` — the request would fail anyway, and the user should see
that before selecting it, not after.

**Cache the encoder.** `tiktoken.get_encoding` is expensive; fetch once at
module level via an `lru_cache`.

**Count the exact text that will be sent.** `compose_message` is the single
source of truth for prompt assembly, shared with the client that actually runs
the completion. If the estimator counted different text, the price shown would
be wrong in a way nobody would ever notice.

**Pass `disallowed_special=()` when encoding.** tiktoken raises on text
containing `<|endoftext|>`, which is a plausible thing to find in an uploaded
file about machine learning. Arbitrary user documents must not be able to crash
the estimator.

**Degrade when the tokenizer cannot be loaded.** tiktoken downloads its BPE
table on first use. This is a local tool that should still price a run offline,
so a failure falls back to a character heuristic and reports `tokenizer` as
`heuristic` instead of breaking the page.

**Router models have no price to show.** The five `-1` entries from plan 01
carry `None` costs, are excluded from the totals, and are listed in
`unpriced_models` — a `-1` reaching the sum would silently *reduce* the total.

## Acceptance criteria

- Cost for a known token count and known price matches an exact `Decimal`,
  with no floating-point drift.
- Prompt and attachment tokens are both counted.
- A model whose context is smaller than the input is flagged, not silently
  priced.
- A free model (`pricing.prompt == 0`) estimates to exactly `Decimal("0")`.
- Totals equal the sum of the per-model figures.

## Tests

Fake models with round prices so expected values are readable. Assert on
`Decimal` equality, not `pytest.approx` — the point of this module is that the
numbers are exact.

## Out of scope

Reading actual usage back from responses after a run — that belongs with the
streaming client (plan 05), which can report real token counts when OpenRouter
sends them.
