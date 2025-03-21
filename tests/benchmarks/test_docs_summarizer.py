"""Benchmarks for DocsSummarizer class."""

# pylint: disable=W0621

from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from ols import config
from ols.utils import suid
from tests.mock_classes.mock_langchain_interface import mock_langchain_interface
from tests.mock_classes.mock_llama_index import MockLlamaIndex
from tests.mock_classes.mock_llm_loader import mock_llm_loader

conversation_id = suid.get_suid()


@pytest.fixture(scope="function", autouse=True)
def _setup():
    """Set up config for benchmarks."""
    config.reload_from_yaml_file("tests/config/valid_config.yaml")


@pytest.fixture
def summarizer():
    """Prepare document summarizer instance."""
    from ols.src.query_helpers.docs_summarizer import DocsSummarizer

    return DocsSummarizer(llm_loader=mock_llm_loader(None))


@pytest.fixture
def summarizer_no_reference_content():
    """Prepare document summarizer instance without reference content."""
    from ols.src.query_helpers.docs_summarizer import DocsSummarizer

    return DocsSummarizer(
        llm_loader=mock_llm_loader(mock_langchain_interface("test response")())
    )


@pytest.fixture
def rag_index():
    """RAG index to be used by benchmarks."""
    return MockLlamaIndex()


def test_summarize_empty_history(benchmark, rag_index, summarizer):
    """Benchmark for DocsSummarizer using mocked index and query engine."""
    question = "What's the ultimate question with answer 42?"
    history = []  # empty history

    with patch("ols.utils.token_handler.RAG_SIMILARITY_CUTOFF", 0.4):
        # run the benchmark
        benchmark(summarizer.create_response, question, rag_index, history)


def test_summarize_no_history(benchmark, rag_index, summarizer):
    """Benchmark for DocsSummarizer using mocked index and query engine, no history is provided."""
    question = "What's the ultimate question with answer 42?"

    with patch("ols.utils.token_handler.RAG_SIMILARITY_CUTOFF", 0.4):
        # no history is passed into summarize() method
        # run the benchmark
        benchmark(summarizer.create_response, question, rag_index)


def test_summarize_history_provided(benchmark, rag_index, summarizer):
    """Benchmark for DocsSummarizer using mocked index and query engine, history is provided."""
    question = "What's the ultimate question with answer 42?"
    history = [HumanMessage("What is Kubernetes?")]

    with patch("ols.utils.token_handler.RAG_SIMILARITY_CUTOFF", 0.4):
        # first call with history provided
        benchmark(summarizer.create_response, question, rag_index, history)


def test_summarize_history_truncation(benchmark, rag_index, summarizer):
    """Benchmark for DocsSummarizer to check if truncation is done."""
    question = "What's the ultimate question with answer 42?"

    # too long history
    history = [HumanMessage("What is Kubernetes?")] * 10

    with patch("ols.utils.token_handler.RAG_SIMILARITY_CUTOFF", 0.4):
        # run the benchmark
        benchmark(summarizer.create_response, question, rag_index, history)


def try_to_run_summarizer(summarizer, question, rag_index, history):
    """Try to run summarizer with expect it will fail."""
    with pytest.raises(Exception):
        summarizer.summarize(conversation_id, question, rag_index, history)


def test_summarize_too_long_question(benchmark, rag_index, summarizer):
    """Benchmark for DocsSummarizer to check if truncation is done."""
    question = "What's the ultimate question with answer 42?" * 10000

    # short history
    history = ["What is Kubernetes?"]

    with patch("ols.utils.token_handler.RAG_SIMILARITY_CUTOFF", 0.4):
        # run the benchmark
        benchmark(try_to_run_summarizer, summarizer, question, rag_index, history)


def test_summarize_too_long_question_long_history(benchmark, rag_index, summarizer):
    """Benchmark for DocsSummarizer to check if truncation is done."""
    question = "What's the ultimate question with answer 42?" * 10000

    # too long history
    history = ["What is Kubernetes?"] * 10000

    with patch("ols.utils.token_handler.RAG_SIMILARITY_CUTOFF", 0.4):
        # run the benchmark
        benchmark(try_to_run_summarizer, summarizer, question, rag_index, history)


def test_summarize_no_reference_content(benchmark, summarizer_no_reference_content):
    """Benchmark for DocsSummarizer using mocked index and query engine."""
    question = "What's the ultimate question with answer 42?"

    # run the benchmark
    benchmark(summarizer_no_reference_content.create_response, question)
