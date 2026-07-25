# Implementation plans

One document per feature, small enough to implement, review and commit on its
own. Plans are numbered in dependency order — later ones assume the earlier
ones have landed.

## Workflow

1. Pick the lowest-numbered plan still in this directory.
2. Implement it, including its tests.
3. Make sure the full check suite passes:

   ```bash
   ruff check . && ruff format --check . && mypy && pytest
   ```

4. Move the plan to [`done/`](done/) in the same commit that implements it, so
   the repository always shows what is built and what is pending.

## Status

| # | Plan | Depends on |
|---|------|-----------|
| 01 | ~~[Settings and model catalog](done/01-config-and-catalog.md)~~ — done | — |
| 02 | ~~[Presets](done/02-presets.md)~~ — done | 01 |
| 03 | ~~[Attachment text extraction](done/03-attachments.md)~~ — done | — |
| 04 | ~~[Token and cost estimation](done/04-estimate.md)~~ — done | 01, 03 |
| 05 | [OpenRouter streaming client](05-openrouter-client.md) | 01 |
| 06 | [Fan-out orchestrator](06-orchestrator.md) | 05 |
| 07 | [Web interface](07-web-ui.md) | 02, 04, 06 |

## Conventions

- Everything in the repository is written in English.
- Money is `decimal.Decimal`, never `float`.
- Anything reaching the network is behind an interface that tests can fake;
  the test suite never makes real HTTP calls.
