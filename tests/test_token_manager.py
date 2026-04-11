"""Tests for TokenManager — Tekken tokenizer integration."""

import pytest
from turbine.token_manager import TokenManager


@pytest.fixture(scope="module")
def tm():
    return TokenManager()


def test_count_returns_positive_int(tm):
    assert tm.count("hello world") > 0


def test_count_scales_with_length(tm):
    short = tm.count("hi")
    long = tm.count("hi " * 100)
    assert long > short


def test_fits_short_text(tm):
    assert tm.fits("hello") is True


def test_fits_respects_reserve(tm):
    # A text that fits with default reserve should not fit if we reserve the whole window
    assert tm.fits("hello", reserve=tm.limit) is False


def test_truncate_to_fit_returns_subset(tm):
    # Each chunk is ~200 chars; 10_000 of them exceeds any context window
    chunks = [f"chunk number {i} " + ("word " * 40) for i in range(10_000)]
    selected = tm.truncate_to_fit(chunks)
    assert len(selected) < len(chunks)
    assert len(selected) > 0


def test_truncate_to_fit_all_fit(tm):
    chunks = ["tiny"] * 3
    selected = tm.truncate_to_fit(chunks)
    assert selected == chunks


def test_default_model_limit():
    tm = TokenManager("mistral-large-latest")
    assert tm.limit == 131_072


def test_unknown_model_defaults_to_32k():
    tm = TokenManager("some-future-model")
    assert tm.limit == 32768
