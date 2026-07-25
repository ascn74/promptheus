"""Tests for token counting and cost estimation.

Prices here are round numbers so the expected values are readable, and every
assertion is on `Decimal` equality — the whole point of this module is that the
arithmetic is exact.
"""

from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
import tiktoken

from promptheus import estimate
from promptheus.attachments import Attachment
from promptheus.catalog import Model
from promptheus.estimate import (
    compose_message,
    count_tokens,
    estimate_run,
    format_usd,
    tokenizer_name,
)


def make_model(
    model_id: str = "vendor/model",
    prompt_price: str = "0.000001",
    completion_price: str = "0.000002",
    context_length: int = 100_000,
    max_completion_tokens: int | None = None,
) -> Model:
    payload: dict[str, Any] = {
        "id": model_id,
        "name": model_id,
        "context_length": context_length,
        "pricing": {"prompt": prompt_price, "completion": completion_price},
        "top_provider": {"max_completion_tokens": max_completion_tokens},
    }
    return Model.model_validate(payload)


def text_attachment(filename: str, text: str) -> Attachment:
    return Attachment(filename=filename, text=text, kind="text")


# --- token counting ----------------------------------------------------------


def test_counts_tokens() -> None:
    assert count_tokens("hello world") > 0
    assert count_tokens("") == 0


def test_special_token_text_does_not_raise() -> None:
    # A file that merely mentions <|endoftext|> is entirely plausible in ML
    # documentation, and tiktoken raises on it unless told not to.
    assert count_tokens("the sentinel is <|endoftext|> in most tokenizers") > 0


def test_longer_text_counts_more() -> None:
    assert count_tokens("word " * 100) > count_tokens("word")


def test_the_real_tokenizer_is_used_when_available() -> None:
    estimate._encoder.cache_clear()
    try:
        assert tokenizer_name() == "o200k_base"
    finally:
        estimate._encoder.cache_clear()


def test_estimation_survives_an_unavailable_tokenizer() -> None:
    """tiktoken downloads its table on first use; offline must still price."""
    estimate._encoder.cache_clear()
    try:
        with mock.patch.object(tiktoken, "get_encoding", side_effect=OSError("offline")):
            assert tokenizer_name() == "heuristic"
            assert count_tokens("hello world") > 0

            result = estimate_run("word " * 100, [], [make_model()])

        assert result.tokenizer == "heuristic"
        assert result.input_tokens > 0
        assert result.max_total_cost > 0
    finally:
        estimate._encoder.cache_clear()


# --- message composition -----------------------------------------------------


def test_prompt_alone_is_unchanged() -> None:
    assert compose_message("Summarise this", []) == "Summarise this"


def test_attachments_are_wrapped_and_named() -> None:
    message = compose_message("Review", [text_attachment("main.py", "print()")])

    assert "Review" in message
    assert '<attachment name="main.py">' in message
    assert "print()" in message
    assert "</attachment>" in message


def test_empty_attachments_are_left_out() -> None:
    message = compose_message(
        "Review",
        [text_attachment("scan.pdf", "   "), text_attachment("real.txt", "content")],
    )

    # A scanned PDF that extracted to nothing would cost tokens and tell the
    # model nothing; the user already saw the extraction warning.
    assert "scan.pdf" not in message
    assert "real.txt" in message


def test_quotes_in_filenames_do_not_break_the_wrapper() -> None:
    message = compose_message("x", [text_attachment('od"d.txt', "body")])

    assert message.count('"') == 2


def test_estimate_counts_prompt_and_attachments_together() -> None:
    models = [make_model()]
    prompt_only = estimate_run("Summarise", [], models)
    with_file = estimate_run("Summarise", [text_attachment("a.txt", "word " * 200)], models)

    assert with_file.input_tokens > prompt_only.input_tokens


# --- cost --------------------------------------------------------------------


def test_cost_is_exact_with_no_floating_point_drift() -> None:
    # 0.0000000938 is the kind of figure a float mangles.
    model = make_model(prompt_price="0.0000000938", completion_price="0.0000001876")
    estimate = estimate_run("x", [], [model], max_output_tokens=1000)

    tokens = estimate.input_tokens
    assert estimate.per_model[0].input_cost == Decimal("0.0000000938") * tokens
    assert estimate.per_model[0].max_output_cost == Decimal("0.0000001876") * 1000


def test_a_free_model_costs_exactly_zero() -> None:
    model = make_model(prompt_price="0", completion_price="0")
    estimate = estimate_run("some prompt", [], [model])

    assert estimate.per_model[0].input_cost == Decimal("0")
    assert estimate.per_model[0].max_output_cost == Decimal("0")
    assert estimate.max_total_cost == Decimal("0")


def test_totals_are_the_sum_of_the_parts() -> None:
    models = [
        make_model("a/one", "0.000001", "0.000002"),
        make_model("b/two", "0.000010", "0.000020"),
        make_model("c/three", "0.000100", "0.000200"),
    ]
    estimate = estimate_run("prompt", [], models)

    assert estimate.input_cost == sum(
        (item.input_cost for item in estimate.per_model if item.input_cost is not None),
        Decimal(0),
    )
    assert estimate.max_output_cost == sum(
        (item.max_output_cost for item in estimate.per_model if item.max_output_cost is not None),
        Decimal(0),
    )
    assert estimate.max_total_cost == estimate.input_cost + estimate.max_output_cost


def test_output_cost_uses_the_requested_ceiling() -> None:
    model = make_model(completion_price="0.000002")
    estimate = estimate_run("x", [], [model], max_output_tokens=500)

    assert estimate.per_model[0].max_output_tokens == 500
    assert estimate.per_model[0].max_output_cost == Decimal("0.000002") * 500


def test_a_model_capping_output_below_the_request_is_respected() -> None:
    model = make_model(max_completion_tokens=128)
    estimate = estimate_run("x", [], [model], max_output_tokens=4096)

    assert estimate.per_model[0].max_output_tokens == 128


def test_a_model_without_a_declared_cap_uses_the_request() -> None:
    # 45 real catalogue entries report no max_completion_tokens.
    model = make_model(max_completion_tokens=None)
    estimate = estimate_run("x", [], [model], max_output_tokens=4096)

    assert estimate.per_model[0].max_output_tokens == 4096


# --- variable pricing --------------------------------------------------------


def test_router_models_report_unknown_rather_than_negative_cost() -> None:
    router = make_model("openrouter/auto", prompt_price="-1", completion_price="-1")
    normal = make_model("vendor/normal", prompt_price="0.000001")

    estimate = estimate_run("prompt", [], [router, normal])

    router_estimate, normal_estimate = estimate.per_model
    assert not router_estimate.has_known_price
    assert router_estimate.input_cost is None
    assert router_estimate.max_total_cost is None
    assert normal_estimate.has_known_price
    assert estimate.unpriced_models == ("openrouter/auto",)


def test_totals_exclude_models_with_unknown_prices() -> None:
    router = make_model("openrouter/auto", prompt_price="-1", completion_price="-1")
    normal = make_model("vendor/normal", prompt_price="0.000001", completion_price="0.000002")

    estimate = estimate_run("prompt", [], [router, normal])

    # A -1 leaking into the sum would silently reduce the total.
    assert estimate.input_cost > 0
    assert estimate.input_cost == estimate.per_model[1].input_cost


# --- context -----------------------------------------------------------------


def test_input_larger_than_the_context_window_is_flagged() -> None:
    small = make_model("vendor/small", context_length=10)
    large = make_model("vendor/large", context_length=1_000_000)

    estimate = estimate_run("word " * 500, [], [small, large])

    assert estimate.per_model[0].exceeds_context
    assert not estimate.per_model[1].exceeds_context
    assert estimate.overflowing_models == ("vendor/small",)


def test_an_overflowing_model_is_still_priced_not_skipped() -> None:
    model = make_model(context_length=10)
    estimate = estimate_run("word " * 500, [], [model])

    # The point is to inform, not to hide the model.
    assert estimate.per_model[0].input_cost is not None
    assert estimate.per_model[0].input_cost > 0


# --- reporting ---------------------------------------------------------------


def test_input_tokens_allow_for_the_chat_template() -> None:
    # Measured against the live API: an 11-token prompt was billed as 15, 16
    # and 27 prompt tokens depending on the model. The gap is the role markers
    # the template adds, which never appear in the text we count.
    result = estimate_run("hello", [], [make_model()])

    assert result.input_tokens == count_tokens("hello") + estimate.MESSAGE_TOKEN_OVERHEAD


def test_estimates_are_always_labelled_approximate() -> None:
    estimate = estimate_run("x", [], [make_model()])

    # One tokenizer stands in for every model family, so this can never be
    # presented as exact.
    assert estimate.approximate is True
    assert estimate.tokenizer


def test_an_empty_selection_produces_zero_totals() -> None:
    estimate = estimate_run("prompt", [], [])

    assert estimate.per_model == ()
    assert estimate.input_cost == Decimal("0")
    assert estimate.max_total_cost == Decimal("0")


# --- display -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", "$0.00"),
        ("1.5", "$1.50"),
        ("0.04237", "$0.04"),
        ("0.0042", "$0.0042"),
        ("0.000042", "$0.000042"),
        ("0.0000004", "<$0.000001"),
    ],
)
def test_format_usd_keeps_small_figures_visible(value: str, expected: str) -> None:
    # A fixed two decimals would render most of the catalogue as $0.00.
    assert format_usd(Decimal(value)) == expected
