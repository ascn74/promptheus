# Ideas not yet planned

Candidates for future plans. Nothing here is committed to, and none of it is
detailed enough to implement — each entry needs its own document under
`plans/` first, following the same shape as the numbered plans.

Ordered roughly by how much they would change the product, not by priority.

---

## Comparison history

Runs are currently ephemeral by design: the registry is process memory with a
TTL, and `Run` dies with the server. Keeping past comparisons would mean
introducing storage, which reverses the "no persistence" decision taken at the
outset — so it needs an explicit decision, not a quiet feature.

Questions a plan would have to answer: SQLite or files; whether the prompt and
attachments are stored alongside the answers, given attachments may be
confidential; how long history is kept; whether a stored run can be re-run
against today's catalogue when the model ids may have been retired.

## Diffing answers

The point of putting answers side by side is to compare them, and the interface
currently offers nothing beyond reading. Worth exploring: highlighting where
two answers agree, marking a preferred answer, and re-running one column with a
different model without redoing the whole comparison.

Hard part: a word diff between free-form prose is often noise. A plan should
establish what question the diff answers before choosing a technique.

## Per-model provider override

The `/models/{id}/endpoints` route exists and works — measured in plan 01 — but
nothing in the interface uses it. Around half the catalogue has a single
provider, so the control should appear only for the models that have more than
one, fetched on expand.

Where it matters: open-weight models, where providers differ in quantisation,
context window and throughput. Comparing the same model across two providers is
a legitimate comparison in its own right.

Related settings already carried by `RoutingOptions` and unused by the UI:
`sort` by price/throughput/latency, and `data_collection: deny` — the latter is
a genuine privacy control for a tool that uploads your documents.

## Images as attachments

Attachments are reduced to text today, deliberately, so every model reads the
same input. But 183 catalogue models accept images, and for those a comparison
of what they *see* is a different and useful question.

The tension is with fairness: a run mixing vision and text-only models is no
longer one prompt to N models. A plan would have to decide whether such a run
is refused, warned about, or split.

## Cost accuracy

Plan 04 claimed input cost was exact; plan 06 proved it is not, because each
provider tokenises differently and the chat template adds markers we never see.
`MESSAGE_TOKEN_OVERHEAD` is a blunt constant.

Worth investigating: whether per-family tokenizers are worth loading given
`architecture.tokenizer` is already in the catalogue, and whether reconciling
the estimate against the real `usage` figures after a run could calibrate it.

## Saving and sharing a comparison

Exporting a finished run as markdown or HTML, so a comparison can leave the
tool. Cheap next to history, since the answers are already rendered — but it
needs a decision about whether the export carries the prompt and attachments.

## Scanned PDFs

Extraction detects a PDF with no text layer and warns rather than silently
sending nothing. OCR was ruled out of plan 03 on scope. If it ever comes back,
it should stay optional: OCR is slow and wrong often enough that it should not
run without the user asking.

---

## Housekeeping

- **`presets.toml` is edited by hand.** Fine by design, but a stale model id is
  only discovered at startup. A `promptheus check` command could validate it
  against the live catalogue in CI.
- **No `/healthz` or version endpoint.** Not needed for a local tool; would be
  needed the day anyone runs this behind anything.
- **The catalogue TTL is one hour and refreshes on demand.** A long-lived
  process shows a stale list for up to an hour after a vendor ships a model.
