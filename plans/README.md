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
| 05 | ~~[OpenRouter streaming client](done/05-openrouter-client.md)~~ — done | 01 |
| 06 | ~~[Fan-out orchestrator](done/06-orchestrator.md)~~ — done | 05 |
| 07 | ~~[Web interface](done/07-web-ui.md)~~ — done | 02, 04, 06 |
| 08 | ~~[UI toolchain: Tailwind and JavaScript](done/08-ui-toolchain.md)~~ — done | 07 |
| 09 | [UX improvements](09-ux-improvements.md) | 08 |

## Conventions

- Everything in the repository is written in English.
- Money is `decimal.Decimal`, never `float`.
- Anything reaching the network is behind an interface that tests can fake;
  the test suite never makes real HTTP calls.
- The interface is styled with Tailwind and may use first-party JavaScript.
  This reverses a decision made in plan 07; see [plan 08](done/08-ui-toolchain.md).
