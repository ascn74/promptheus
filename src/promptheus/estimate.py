"""Tell the user what a run will cost before they start it.

Both sides of the figure are estimates, for different reasons. The output side
is a genuine ceiling — capped by `max_tokens`, and measured against the live
API as holding exactly. The input side cannot be exact: we know the text we
send, but not how each provider tokenises it, and the chat template wraps every
message in role markers we never see. Measured against the live API on an
11-token prompt, the real prompt count came back as 15, 16 and 27 tokens
depending on the model.

The one thing this module refuses to do is touch a `float`. Prices are
per-token decimals small enough that binary floating point loses them, and the
loss would only surface on a bill.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

import tiktoken

from promptheus.attachments import Attachment
from promptheus.catalog import Model

logger = logging.getLogger(__name__)

ENCODING_NAME = "o200k_base"
HEURISTIC_NAME = "heuristic"

DEFAULT_MAX_OUTPUT_TOKENS = 4096

# Used only when the real encoder cannot be loaded. Roughly right for English
# prose and far better than refusing to show a number at all.
_CHARS_PER_TOKEN = 4

# The chat template wraps each message in role markers that never appear in the
# text we count. Measured against the live API, the gap was 4 to 16 tokens
# depending on the model family. On a realistic prompt this is well under 1%;
# on a one-line prompt it is most of the difference.
MESSAGE_TOKEN_OVERHEAD = 8


@dataclass(frozen=True)
class ModelEstimate:
    model_id: str
    #: Approximate: includes the chat-template allowance, not the provider's
    #: own tokenizer.
    input_tokens: int
    max_output_tokens: int
    context_length: int
    exceeds_context: bool
    # None when the model prices variably: the OpenRouter routers report -1
    # because they only pick a model at request time.
    input_cost: Decimal | None
    max_output_cost: Decimal | None

    @property
    def has_known_price(self) -> bool:
        return self.input_cost is not None

    @property
    def max_total_cost(self) -> Decimal | None:
        if self.input_cost is None or self.max_output_cost is None:
            return None
        return self.input_cost + self.max_output_cost


@dataclass(frozen=True)
class RunEstimate:
    per_model: tuple[ModelEstimate, ...]
    #: Includes `MESSAGE_TOKEN_OVERHEAD`; still an approximation.
    input_tokens: int
    #: Totals cover only the models whose price is known.
    input_cost: Decimal
    max_output_cost: Decimal
    tokenizer: str
    approximate: bool = True

    @property
    def max_total_cost(self) -> Decimal:
        return self.input_cost + self.max_output_cost

    @property
    def unpriced_models(self) -> tuple[str, ...]:
        return tuple(item.model_id for item in self.per_model if not item.has_known_price)

    @property
    def overflowing_models(self) -> tuple[str, ...]:
        return tuple(item.model_id for item in self.per_model if item.exceeds_context)


@lru_cache(maxsize=1)
def _encoder() -> tiktoken.Encoding | None:
    """Load the tokenizer once, or give up quietly.

    tiktoken downloads its BPE table on first use. Promptheus is a local tool
    that should still be able to price a run offline, so a failure here
    degrades to a character heuristic rather than breaking the page.
    """
    try:
        return tiktoken.get_encoding(ENCODING_NAME)
    # Deliberately broad: a download failure, a read-only cache directory and a
    # corrupt BPE file should all degrade rather than break the page.
    except Exception:
        logger.warning(
            "could not load the %s tokenizer; falling back to a character heuristic",
            ENCODING_NAME,
            exc_info=True,
        )
        return None


def tokenizer_name() -> str:
    return ENCODING_NAME if _encoder() is not None else HEURISTIC_NAME


def count_tokens(text: str) -> int:
    encoder = _encoder()
    if encoder is None:
        return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN
    # Attachments are arbitrary user files. Without `disallowed_special=()` a
    # document that merely mentions <|endoftext|> raises, which is a plausible
    # thing to find in ML documentation or a dataset.
    return len(encoder.encode(text, disallowed_special=()))


def compose_message(prompt: str, attachments: Sequence[Attachment]) -> str:
    """Build the exact text that will be sent to every model.

    This is the single source of truth for prompt assembly: if the estimator
    counted different text from what the client sends, the price shown would be
    wrong in a way nobody would ever notice.
    """
    blocks = [prompt.strip()] if prompt.strip() else []
    for attachment in attachments:
        # An attachment that extracted to nothing — a scanned PDF, say — is
        # left out. It would cost tokens and tell the model nothing; the user
        # already sees the extraction warning before running.
        if attachment.is_empty:
            continue
        name = attachment.filename.replace('"', "'")
        blocks.append(f'<attachment name="{name}">\n{attachment.text}\n</attachment>')
    return "\n\n".join(blocks)


def estimate_run(
    prompt: str,
    attachments: Sequence[Attachment],
    models: Sequence[Model],
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> RunEstimate:
    """Price a prospective run.

    The output side is a true ceiling: no model can bill for more than
    `max_tokens`. The input side is an approximation — see the module
    docstring — so the total can be exceeded slightly on very short prompts.
    """
    text = compose_message(prompt, attachments)
    input_tokens = count_tokens(text) + MESSAGE_TOKEN_OVERHEAD

    per_model: list[ModelEstimate] = []
    total_input = Decimal(0)
    total_max_output = Decimal(0)

    for model in models:
        # A model may cap output below what we asked for.
        model_cap = model.top_provider.max_completion_tokens
        output_tokens = min(max_output_tokens, model_cap) if model_cap else max_output_tokens

        if model.pricing.is_variable:
            input_cost: Decimal | None = None
            output_cost: Decimal | None = None
        else:
            input_cost = model.pricing.prompt * input_tokens
            output_cost = model.pricing.completion * output_tokens
            total_input += input_cost
            total_max_output += output_cost

        per_model.append(
            ModelEstimate(
                model_id=model.id,
                input_tokens=input_tokens,
                max_output_tokens=output_tokens,
                context_length=model.context_length,
                exceeds_context=input_tokens > model.context_length,
                input_cost=input_cost,
                max_output_cost=output_cost,
            )
        )

    return RunEstimate(
        per_model=tuple(per_model),
        input_tokens=input_tokens,
        input_cost=total_input,
        max_output_cost=total_max_output,
        tokenizer=tokenizer_name(),
    )


def format_usd(value: Decimal) -> str:
    """Render a cost for display, rounding only here.

    Costs span several orders of magnitude — a cheap model on a short prompt
    lands near $0.000002 — so a fixed two decimals would show most of the
    catalog as $0.00.
    """
    if value == 0:
        return "$0.00"
    if value >= Decimal("0.01"):
        return f"${_round(value, '0.01')}"
    if value >= Decimal("0.0001"):
        return f"${_round(value, '0.0001')}"
    if value >= Decimal("0.000001"):
        return f"${_round(value, '0.000001')}"
    return "<$0.000001"


def _round(value: Decimal, exponent: str) -> Decimal:
    return value.quantize(Decimal(exponent), rounding=ROUND_HALF_UP)
