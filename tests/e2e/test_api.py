"""Integration tests for basic OLS REST API endpoints."""

import json
import os
import re
import time
from argparse import Namespace

import pytest
import requests
from httpx import Client

from ols.constants import HTTP_REQUEST_HEADERS_TO_REDACT
from ols.utils import suid
from scripts.evaluation.response_evaluation import ResponseEvaluation
from tests.e2e.utils import client as client_utils
from tests.e2e.utils import cluster as cluster_utils
from tests.e2e.utils import metrics as metrics_utils
from tests.e2e.utils import ols_installer
from tests.e2e.utils import response as response_utils
from tests.e2e.utils.constants import (
    BASIC_ENDPOINTS_TIMEOUT,
    CONVERSATION_ID,
    LLM_REST_API_TIMEOUT,
    NON_LLM_REST_API_TIMEOUT,
    OLS_COLLECTOR_DISABLING_FILE,
    OLS_USER_DATA_COLLECTION_INTERVAL,
    OLS_USER_DATA_PATH,
)
from tests.e2e.utils.decorators import retry
from tests.e2e.utils.postgres import (
    read_conversation_history,
    read_conversation_history_count,
    retrieve_connection,
)
from tests.e2e.utils.wait_for_ols import wait_for_ols
from tests.scripts.must_gather import must_gather

# on_cluster is set to true when the tests are being run
# against ols running on a cluster
on_cluster = False


# generic http client for talking to OLS, when OLS is run on a cluster
# this client will be preconfigured with a valid user token header.
client: Client
metrics_client: Client


OLS_READY = False


def setup_module(module):
    """Set up common artifacts used by all e2e tests."""
    global client, metrics_client, OLS_READY, on_cluster
    provider = os.getenv("PROVIDER")

    # OLS_URL env only needs to be set when running against a local ols instance,
    # when ols is run against a cluster the url is retrieved from the cluster.
    ols_url = os.getenv("OLS_URL", "")
    if "localhost" not in ols_url:
        on_cluster = True

    if on_cluster:
        try:
            ols_url, token, metrics_token = ols_installer.install_ols()
        except Exception as e:
            print(f"Error setting up OLS on cluster: {e}")
            must_gather()
            raise e
    else:
        print("Setting up for standalone test execution\n")
        # these variables must be created, but does not have to contain
        # anything relevant for local testing (values are optional)
        token = None
        metrics_token = None

    client = client_utils.get_http_client(ols_url, token)
    metrics_client = client_utils.get_http_client(ols_url, metrics_token)

    # Wait for OLS to be ready
    print(f"Waiting for OLS to be ready at url: {ols_url} with provider: {provider}...")
    OLS_READY = wait_for_ols(ols_url)
    print(f"OLS is ready: {OLS_READY}")
    if not OLS_READY:
        must_gather()


def teardown_module(module):
    """Clean up the environment after all tests are executed."""
    if on_cluster:
        must_gather()


@pytest.fixture(scope="module")
def postgres_connection():
    """Fixture with Postgres connection."""
    return retrieve_connection()


@pytest.mark.smoketest()
@retry(max_attempts=3, wait_between_runs=10)
def test_readiness():
    """Test handler for /readiness REST API endpoint."""
    endpoint = "/readiness"
    with metrics_utils.RestAPICallCounterChecker(metrics_client, endpoint):
        response = client.get(endpoint, timeout=LLM_REST_API_TIMEOUT)
        assert response.status_code == requests.codes.ok
        response_utils.check_content_type(response, "application/json")
        assert response.json() == {"ready": True, "reason": "service is ready"}


@pytest.mark.smoketest()
def test_liveness():
    """Test handler for /liveness REST API endpoint."""
    endpoint = "/liveness"
    with metrics_utils.RestAPICallCounterChecker(metrics_client, endpoint):
        response = client.get(endpoint, timeout=BASIC_ENDPOINTS_TIMEOUT)
        assert response.status_code == requests.codes.ok
        response_utils.check_content_type(response, "application/json")
        assert response.json() == {"alive": True}


def test_invalid_question():
    """Check the REST API /v1/query with POST HTTP method for invalid question."""
    endpoint = "/v1/query"
    with metrics_utils.RestAPICallCounterChecker(metrics_client, endpoint):
        cid = suid.get_suid()
        response = client.post(
            endpoint,
            json={"conversation_id": cid, "query": "how to make burger?"},
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.ok

        response_utils.check_content_type(response, "application/json")
        print(vars(response))

        json_response = response.json()
        assert json_response["conversation_id"] == cid
        assert json_response["referenced_documents"] == []
        assert not json_response["truncated"]
        # LLM shouldn't answer non-ocp queries or
        # at least acknowledges that query is non-ocp.
        # Below assert is minimal due to model randomness.
        assert re.search(
            r"(sorry|questions|assist)",
            json_response["response"],
            re.IGNORECASE,
        )


def test_invalid_question_without_conversation_id():
    """Check the REST API /v1/query with invalid question and without conversation ID."""
    endpoint = "/v1/query"
    with metrics_utils.RestAPICallCounterChecker(metrics_client, endpoint):
        response = client.post(
            endpoint,
            json={"query": "how to make burger?"},
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.ok

        response_utils.check_content_type(response, "application/json")
        print(vars(response))

        json_response = response.json()
        assert json_response["referenced_documents"] == []
        assert json_response["truncated"] is False
        # Query classification is disabled by default,
        # and we rely on the model (controlled by prompt) to reject non-ocp queries.
        # Randomness in response is expected.
        # assert json_response["response"] == INVALID_QUERY_RESP
        assert re.search(
            r"(sorry|questions|assist)",
            json_response["response"],
            re.IGNORECASE,
        )

        # new conversation ID should be generated
        assert suid.check_suid(
            json_response["conversation_id"]
        ), "Conversation ID is not in UUID format"


def test_query_call_without_payload():
    """Check the REST API /v1/query with POST HTTP method when no payload is provided."""
    endpoint = "/v1/query"
    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.unprocessable_entity
    ):
        response = client.post(
            endpoint,
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.unprocessable_entity

        response_utils.check_content_type(response, "application/json")
        print(vars(response))
        # the actual response might differ when new Pydantic version will be used
        # so let's do just primitive check
        assert "missing" in response.text


def test_query_call_with_improper_payload():
    """Check the REST API /v1/query with POST HTTP method when improper payload is provided."""
    endpoint = "/v1/query"
    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.unprocessable_entity
    ):
        response = client.post(
            endpoint,
            json={"parameter": "this-is-not-proper-question-my-friend"},
            timeout=NON_LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.unprocessable_entity

        response_utils.check_content_type(response, "application/json")
        print(vars(response))
        # the actual response might differ when new Pydantic version will be used
        # so let's do just primitive check
        assert "missing" in response.text


def test_valid_question_improper_conversation_id() -> None:
    """Check the REST API /v1/query with POST HTTP method for improper conversation ID."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.internal_server_error
    ):
        response = client.post(
            endpoint,
            json={"conversation_id": "not-uuid", "query": "what is kubernetes?"},
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.internal_server_error

        response_utils.check_content_type(response, "application/json")
        json_response = response.json()
        expected_response = {
            "detail": {
                "response": "Error retrieving conversation history",
                "cause": "Invalid conversation ID not-uuid",
            }
        }
        assert json_response == expected_response


@retry(max_attempts=3, wait_between_runs=10)
def test_valid_question_missing_conversation_id() -> None:
    """Check the REST API /v1/query with POST HTTP method for missing conversation ID."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.ok
    ):
        response = client.post(
            endpoint,
            json={"conversation_id": "", "query": "what is kubernetes?"},
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.ok

        response_utils.check_content_type(response, "application/json")
        json_response = response.json()

        # new conversation ID should be returned
        assert (
            "conversation_id" in json_response
        ), "New conversation ID was not generated"
        assert suid.check_suid(
            json_response["conversation_id"]
        ), "Conversation ID is not in UUID format"


def test_too_long_question() -> None:
    """Check the REST API /v1/query with too long question."""
    endpoint = "/v1/query"
    # let's make the query really large, larger that context window size
    query = "what is kubernetes?" * 10000

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.request_entity_too_large
    ):
        cid = suid.get_suid()
        response = client.post(
            endpoint,
            json={"conversation_id": cid, "query": query},
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.request_entity_too_large

        response_utils.check_content_type(response, "application/json")
        print(vars(response))
        json_response = response.json()
        assert "detail" in json_response
        assert json_response["detail"]["response"] == "Prompt is too long"


@pytest.mark.smoketest()
@pytest.mark.rag()
def test_valid_question() -> None:
    """Check the REST API /v1/query with POST HTTP method for valid question and no yaml."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(metrics_client, endpoint):
        cid = suid.get_suid()
        response = client.post(
            endpoint,
            json={"conversation_id": cid, "query": "what is kubernetes?"},
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.ok

        response_utils.check_content_type(response, "application/json")
        print(vars(response))
        json_response = response.json()

        # checking a few major information from response
        assert json_response["conversation_id"] == cid
        assert "Kubernetes is" in json_response["response"]
        assert re.search(
            r"orchestration (tool|system|platform|engine)",
            json_response["response"],
            re.IGNORECASE,
        )


@pytest.mark.rag()
def test_ocp_docs_version_same_as_cluster_version() -> None:
    """Check that the version of OCP docs matches the cluster we're on."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(metrics_client, endpoint):
        cid = suid.get_suid()
        response = client.post(
            endpoint,
            json={
                "conversation_id": cid,
                "query": "welcome openshift container platform documentation",
            },
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.ok

        response_utils.check_content_type(response, "application/json")
        print(vars(response))
        json_response = response.json()

        major, minor = cluster_utils.get_cluster_version()

        assert len(json_response["referenced_documents"]) > 1
        assert f"{major}.{minor}" in json_response["referenced_documents"][0]["title"]


def test_valid_question_tokens_counter() -> None:
    """Check how the tokens counter are updated accordingly."""
    model, provider = metrics_utils.get_enabled_model_and_provider(metrics_client)

    endpoint = "/v1/query"
    with (
        metrics_utils.RestAPICallCounterChecker(metrics_client, endpoint),
        metrics_utils.TokenCounterChecker(metrics_client, model, provider),
    ):
        response = client.post(
            endpoint,
            json={"query": "what is kubernetes?"},
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.ok
        response_utils.check_content_type(response, "application/json")


def test_invalid_question_tokens_counter() -> None:
    """Check how the tokens counter are updated accordingly."""
    model, provider = metrics_utils.get_enabled_model_and_provider(metrics_client)

    endpoint = "/v1/query"
    with (
        metrics_utils.RestAPICallCounterChecker(metrics_client, endpoint),
        metrics_utils.TokenCounterChecker(metrics_client, model, provider),
    ):
        response = client.post(
            endpoint,
            json={"query": "how to make burger?"},
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.ok
        response_utils.check_content_type(response, "application/json")


def test_token_counters_for_query_call_without_payload() -> None:
    """Check how the tokens counter are updated accordingly."""
    model, provider = metrics_utils.get_enabled_model_and_provider(metrics_client)

    endpoint = "/v1/query"
    with (
        metrics_utils.RestAPICallCounterChecker(
            metrics_client, endpoint, status_code=requests.codes.unprocessable_entity
        ),
        metrics_utils.TokenCounterChecker(
            metrics_client,
            model,
            provider,
            expect_sent_change=False,
            expect_received_change=False,
        ),
    ):
        response = client.post(
            endpoint,
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.unprocessable_entity
        response_utils.check_content_type(response, "application/json")


def test_token_counters_for_query_call_with_improper_payload() -> None:
    """Check how the tokens counter are updated accordingly."""
    model, provider = metrics_utils.get_enabled_model_and_provider(metrics_client)

    endpoint = "/v1/query"
    with (
        metrics_utils.RestAPICallCounterChecker(
            metrics_client, endpoint, status_code=requests.codes.unprocessable_entity
        ),
        metrics_utils.TokenCounterChecker(
            metrics_client,
            model,
            provider,
            expect_sent_change=False,
            expect_received_change=False,
        ),
    ):
        response = client.post(
            endpoint,
            json={"parameter": "this-is-not-proper-question-my-friend"},
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.unprocessable_entity
        response_utils.check_content_type(response, "application/json")


@pytest.mark.rag()
@retry(max_attempts=3, wait_between_runs=10)
def test_rag_question() -> None:
    """Ensure responses include rag references."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(metrics_client, endpoint):
        response = client.post(
            endpoint,
            json={"query": "what is openshift virtualization?"},
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.ok
        response_utils.check_content_type(response, "application/json")

        print(vars(response))
        json_response = response.json()
        assert "conversation_id" in json_response
        assert len(json_response["referenced_documents"]) > 1
        assert "virt" in json_response["referenced_documents"][0]["docs_url"]
        assert "https://" in json_response["referenced_documents"][0]["docs_url"]
        assert json_response["referenced_documents"][0]["title"]

        # Length should be same, as there won't be duplicate entry
        doc_urls_list = [rd["docs_url"] for rd in json_response["referenced_documents"]]
        assert len(doc_urls_list) == len(set(doc_urls_list))


@pytest.mark.cluster()
def test_query_filter() -> None:
    """Ensure responses does not include filtered words and redacted words are not logged."""
    endpoint = "/v1/query"
    with metrics_utils.RestAPICallCounterChecker(metrics_client, endpoint):
        query = "what is foo in bar?"
        response = client.post(
            endpoint,
            json={"query": query},
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.ok
        response_utils.check_content_type(response, "application/json")
        print(vars(response))
        json_response = response.json()
        assert "conversation_id" in json_response
        # values to be filtered and replaced are defined in:
        # tests/config/singleprovider.e2e.template.config.yaml
        response_text = json_response["response"].lower()
        assert "openshift" in response_text
        assert "deploy" in response_text
        response_words = response_text.split()
        assert "foo" not in response_words
        assert "bar" not in response_words

        # Retrieve the pod name
        ols_container_name = "lightspeed-service-api"
        pod_name = cluster_utils.get_pod_by_prefix()[0]

        # Check if filtered words are redacted in the logs
        container_log = cluster_utils.get_container_log(pod_name, ols_container_name)

        # Ensure redacted patterns do not appear in the logs
        unwanted_patterns = ["foo ", "what is foo in bar?"]
        for line in container_log.splitlines():
            # Only check lines that are not part of a query
            if re.search(r'Body: \{"query":', line):
                continue
            # check that the pattern is indeed not found in logs
            for pattern in unwanted_patterns:
                assert pattern not in line.lower()

        # Ensure the intended redaction has occurred
        assert "what is deployment in openshift?" in container_log


@retry(max_attempts=3, wait_between_runs=10)
def test_conversation_history() -> None:
    """Ensure conversations include previous query history."""
    endpoint = "/v1/query"
    with metrics_utils.RestAPICallCounterChecker(metrics_client, endpoint):
        response = client.post(
            endpoint,
            json={
                "query": "what is ingress in kubernetes?",
            },
            timeout=LLM_REST_API_TIMEOUT,
        )
        debug_msg = "First call to LLM without conversation history has failed"
        assert response.status_code == requests.codes.ok, debug_msg
        response_utils.check_content_type(response, "application/json", debug_msg)

        print(vars(response))
        json_response = response.json()
        response_text = json_response["response"].lower()
        assert "ingress" in response_text, debug_msg

        # get the conversation id so we can reuse it for the follow up question
        cid = json_response["conversation_id"]
        response = client.post(
            endpoint,
            json={"conversation_id": cid, "query": "what?"},
            timeout=LLM_REST_API_TIMEOUT,
        )
        print(vars(response))

        debug_msg = "Second call to LLM with conversation history has failed"
        assert response.status_code == requests.codes.ok
        response_utils.check_content_type(response, "application/json", debug_msg)

        json_response = response.json()
        response_text = json_response["response"].lower()
        assert "ingress" in response_text, debug_msg


def test_query_with_provider_but_not_model() -> None:
    """Check the REST API /v1/query with POST HTTP method for provider specified, but no model."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.unprocessable_entity
    ):
        # just the provider is explicitly specified, but model selection is missing
        response = client.post(
            endpoint,
            json={
                "conversation_id": "",
                "query": "what is kubernetes?",
                "provider": "bam",
            },
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.unprocessable_entity
        response_utils.check_content_type(response, "application/json")

        json_response = response.json()

        # error thrown on Pydantic level
        assert (
            json_response["detail"][0]["msg"]
            == "Value error, LLM model must be specified when the provider is specified."
        )


def test_query_with_model_but_not_provider() -> None:
    """Check the REST API /v1/query with POST HTTP method for model specified, but no provider."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.unprocessable_entity
    ):
        # just model is explicitly specified, but provider selection is missing
        response = client.post(
            endpoint,
            json={
                "conversation_id": "",
                "query": "what is kubernetes?",
                "model": "ibm/granite-13b-chat-v2",
            },
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.unprocessable_entity
        response_utils.check_content_type(response, "application/json")

        json_response = response.json()

        assert (
            json_response["detail"][0]["msg"]
            == "Value error, LLM provider must be specified when the model is specified."
        )


def test_query_with_unknown_provider() -> None:
    """Check the REST API /v1/query with POST HTTP method for unknown provider specified."""
    endpoint = "/v1/query"

    # retrieve currently selected model
    model, _ = metrics_utils.get_enabled_model_and_provider(metrics_client)

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.unprocessable_entity
    ):
        # provider is unknown
        response = client.post(
            endpoint,
            json={
                "conversation_id": "",
                "query": "what is kubernetes?",
                "provider": "foo",
                "model": model,
            },
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.unprocessable_entity
        response_utils.check_content_type(response, "application/json")

        json_response = response.json()

        # explicit response and cause check
        assert (
            "detail" in json_response
        ), "Improper response format: 'detail' node is missing"
        assert "Unable to process this request" in json_response["detail"]["response"]
        assert (
            "Provider 'foo' is not a valid provider."
            in json_response["detail"]["cause"]
        )


def test_query_with_unknown_model() -> None:
    """Check the REST API /v1/query with POST HTTP method for unknown model specified."""
    endpoint = "/v1/query"

    # retrieve currently selected provider
    _, provider = metrics_utils.get_enabled_model_and_provider(metrics_client)

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.unprocessable_entity
    ):
        # model is unknown
        response = client.post(
            endpoint,
            json={
                "conversation_id": "",
                "query": "what is kubernetes?",
                "provider": provider,
                "model": "bar",
            },
            timeout=LLM_REST_API_TIMEOUT,
        )
        assert response.status_code == requests.codes.unprocessable_entity
        response_utils.check_content_type(response, "application/json")

        json_response = response.json()

        # explicit response and cause check
        assert (
            "detail" in json_response
        ), "Improper response format: 'detail' node is missing"
        assert "Unable to process this request" in json_response["detail"]["response"]
        assert "Model 'bar' is not a valid model " in json_response["detail"]["cause"]


def test_metrics() -> None:
    """Check if service provides metrics endpoint with expected metrics."""
    response = metrics_client.get("/metrics", timeout=BASIC_ENDPOINTS_TIMEOUT)
    assert response.status_code == requests.codes.ok
    assert response.text is not None

    # counters that are expected to be part of metrics
    expected_counters = (
        "ols_rest_api_calls_total",
        "ols_llm_calls_total",
        "ols_llm_calls_failures_total",
        "ols_llm_validation_errors_total",
        "ols_llm_token_sent_total",
        "ols_llm_token_received_total",
        "ols_provider_model_configuration",
    )

    # check if all counters are present
    for expected_counter in expected_counters:
        assert f"{expected_counter} " in response.text

    # check the duration histogram presence
    assert 'response_duration_seconds_count{path="/metrics"}' in response.text
    assert 'response_duration_seconds_sum{path="/metrics"}' in response.text


def test_model_provider():
    """Read configured model and provider from metrics."""
    model, provider = metrics_utils.get_enabled_model_and_provider(metrics_client)

    # enabled model must be one of our expected combinations
    assert model, provider in {
        ("gpt-4o-mini", "openai"),
        ("gpt-4o-mini", "azure_openai"),
        ("ibm/granite-13b-chat-v2", "bam"),
        ("ibm/granite-13b-chat-v2", "watsonx"),
    }


def test_one_default_model_provider():
    """Check if one model and provider is selected as default."""
    states = metrics_utils.get_enable_status_for_all_models(metrics_client)
    enabled_states = [state for state in states if state is True]
    assert (
        len(enabled_states) == 1
    ), "one model and provider should be selected as default"


@pytest.mark.cluster()
def test_improper_token():
    """Test accessing /v1/query endpoint using improper auth. token."""
    response = client.post(
        "/v1/query",
        json={"query": "what is foo in bar?"},
        timeout=NON_LLM_REST_API_TIMEOUT,
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == requests.codes.forbidden


@pytest.mark.cluster()
def test_forbidden_user():
    """Test scenarios where we expect an unauthorized response.

    Test accessing /v1/query endpoint using the metrics user w/ no ols permissions,
    Test accessing /metrics endpoint using the ols user w/ no ols-metrics permissions.
    """
    response = metrics_client.post(
        "/v1/query",
        json={"query": "what is foo in bar?"},
        timeout=NON_LLM_REST_API_TIMEOUT,
    )
    assert response.status_code == requests.codes.forbidden
    response = client.get("/metrics", timeout=BASIC_ENDPOINTS_TIMEOUT)
    assert response.status_code == requests.codes.forbidden


# TODO OLS-652: This test currently doesn't work in CI. We don't currently know
# how to grant permissions to the service account in the test cluster
# to access clusterversions resource.
# @pytest.mark.cluster()
# def test_get_cluster_id_function():
#     """Test if the cluster ID is properly retrieved."""
#     # During the test in cluster, there is no config initialized for the
#     # tests run (these run against application), so we need to initialize
#     # the config (with the fields auth needs) manually here.
#     config.init_config("tests/config/auth_config.yaml")

#     actual = K8sClientSingleton.get_cluster_id()
#     expected = cluster_utils.get_cluster_id()

#     assert actual == expected


@pytest.mark.cluster()
def test_feedback_can_post_with_wrong_token():
    """Test posting feedback with improper auth. token."""
    response = client.post(
        "/v1/feedback",
        json={
            "conversation_id": CONVERSATION_ID,
            "user_question": "what is OCP4?",
            "llm_response": "Openshift 4 is ...",
            "sentiment": 1,
        },
        timeout=BASIC_ENDPOINTS_TIMEOUT,
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == requests.codes.forbidden


@pytest.mark.cluster()
def test_feedback_storing_cluster():
    """Test if the feedbacks are stored properly."""
    feedbacks_path = OLS_USER_DATA_PATH + "/feedback"
    pod_name = cluster_utils.get_pod_by_prefix()[0]

    # disable collector script to avoid interference with the test
    cluster_utils.create_file(pod_name, OLS_COLLECTOR_DISABLING_FILE, "")

    # there are multiple tests running agains cluster, so transcripts
    # can be already present - we need to ensure the storage is empty
    # for this test
    feedbacks = cluster_utils.list_path(pod_name, feedbacks_path)
    if feedbacks:
        cluster_utils.remove_dir(pod_name, feedbacks_path)
        assert cluster_utils.list_path(pod_name, feedbacks_path) is None

    response = client.post(
        "/v1/feedback",
        json={
            "conversation_id": CONVERSATION_ID,
            "user_question": "what is OCP4?",
            "llm_response": "Openshift 4 is ...",
            "sentiment": 1,
        },
        timeout=BASIC_ENDPOINTS_TIMEOUT,
    )

    assert response.status_code == requests.codes.ok

    feedback_data = cluster_utils.get_single_existing_feedback(pod_name, feedbacks_path)

    assert feedback_data["user_id"]  # we don't care about actual value
    assert feedback_data["conversation_id"] == CONVERSATION_ID
    assert feedback_data["user_question"] == "what is OCP4?"
    assert feedback_data["llm_response"] == "Openshift 4 is ..."
    assert feedback_data["sentiment"] == 1


def test_feedback_missing_conversation_id():
    """Test posting feedback with missing conversation ID."""
    response = client.post(
        "/v1/feedback",
        json={
            "user_question": "what is OCP4?",
            "llm_response": "Openshift 4 is ...",
            "sentiment": 1,
        },
        timeout=BASIC_ENDPOINTS_TIMEOUT,
    )

    response_utils.check_missing_field_response(response, "conversation_id")


def test_feedback_missing_user_question():
    """Test posting feedback with missing user question."""
    response = client.post(
        "/v1/feedback",
        json={
            "conversation_id": CONVERSATION_ID,
            "llm_response": "Openshift 4 is ...",
            "sentiment": 1,
        },
        timeout=BASIC_ENDPOINTS_TIMEOUT,
    )

    response_utils.check_missing_field_response(response, "user_question")


def test_feedback_missing_llm_response():
    """Test posting feedback with missing LLM response."""
    response = client.post(
        "/v1/feedback",
        json={
            "conversation_id": CONVERSATION_ID,
            "user_question": "what is OCP4?",
            "sentiment": 1,
        },
        timeout=BASIC_ENDPOINTS_TIMEOUT,
    )

    response_utils.check_missing_field_response(response, "llm_response")


def test_feedback_improper_conversation_id():
    """Test posting feedback with improper conversation ID."""
    response = client.post(
        "/v1/feedback",
        json={
            "conversation_id": "incorrect-conversation-id",
            "user_question": "what is OCP4?",
            "llm_response": "Openshift 4 is ...",
            "sentiment": 1,
        },
        timeout=BASIC_ENDPOINTS_TIMEOUT,
    )

    # error should be detected on Pydantic level
    assert response.status_code == requests.codes.unprocessable

    # for incorrect conversation ID, the payload should be valid JSON
    response_utils.check_content_type(response, "application/json")
    json_response = response.json()

    assert (
        "detail" in json_response
    ), "Improper response format: 'detail' node is missing"
    assert (
        json_response["detail"][0]["msg"]
        == "Value error, Improper conversation ID incorrect-conversation-id"
    )


@pytest.mark.cluster()
def test_transcripts_storing_cluster():
    """Test if the transcripts are stored properly."""
    transcripts_path = OLS_USER_DATA_PATH + "/transcripts"
    pod_name = cluster_utils.get_pod_by_prefix()[0]

    # disable collector script to avoid interference with the test
    cluster_utils.create_file(pod_name, OLS_COLLECTOR_DISABLING_FILE, "")

    # there are multiple tests running agains cluster, so transcripts
    # can be already present - we need to ensure the storage is empty
    # for this test
    transcripts = cluster_utils.list_path(pod_name, transcripts_path)
    if transcripts:
        cluster_utils.remove_dir(pod_name, transcripts_path)
        assert cluster_utils.list_path(pod_name, transcripts_path) is None

    response = client.post(
        "/v1/query",
        json={
            "query": "what is kubernetes?",
            "attachments": [
                {
                    "attachment_type": "log",
                    "content_type": "text/plain",
                    # Sample content
                    "content": "Kubernetes is a core component of OpenShift.",
                }
            ],
        },
        timeout=LLM_REST_API_TIMEOUT,
    )
    assert response.status_code == requests.codes.ok

    transcript = cluster_utils.get_single_existing_transcript(
        pod_name, transcripts_path
    )

    assert transcript["metadata"]  # just check if it is not empty
    assert transcript["redacted_query"] == "what is kubernetes?"
    # we don't want llm response influence this test
    assert "query_is_valid" in transcript
    assert "llm_response" in transcript
    assert "rag_chunks" in transcript

    assert transcript["query_is_valid"]
    assert isinstance(transcript["rag_chunks"], list)
    assert len(transcript["rag_chunks"])
    assert transcript["rag_chunks"][0]["text"]
    assert transcript["rag_chunks"][0]["doc_url"]
    assert transcript["rag_chunks"][0]["doc_title"]
    assert "truncated" in transcript

    # check the attachment node existence and its content
    assert "attachments" in transcript

    expected_attachment_node = [
        {
            "attachment_type": "log",
            "content_type": "text/plain",
            "content": "Kubernetes is a core component of OpenShift.",
        }
    ]
    assert transcript["attachments"] == expected_attachment_node


@retry(max_attempts=3, wait_between_runs=10)
def test_openapi_endpoint():
    """Test handler for /opanapi REST API endpoint."""
    response = client.get("/openapi.json", timeout=BASIC_ENDPOINTS_TIMEOUT)
    assert response.status_code == requests.codes.ok
    response_utils.check_content_type(response, "application/json")

    payload = response.json()
    assert payload is not None, "Incorrect response"

    # check the metadata nodes
    for attribute in ("openapi", "info", "components", "paths"):
        assert (
            attribute in payload
        ), f"Required metadata attribute {attribute} not found"

    # check application description
    info = payload["info"]
    assert "description" in info, "Service description not provided"
    assert "OpenShift LightSpeed Service API specification" in info["description"]

    # elementary check that all mandatory endpoints are covered
    paths = payload["paths"]
    for endpoint in ("/readiness", "/liveness", "/v1/query", "/v1/feedback"):
        assert endpoint in paths, f"Endpoint {endpoint} is not described"

    # retrieve pre-generated OpenAPI schema
    with open("docs/openapi.json") as fin:
        expected_schema = json.load(fin)

    # remove node that is not included in pre-generated OpenAPI schema
    del payload["info"]["license"]

    # compare schemas (as dicts)
    assert (
        payload == expected_schema
    ), "OpenAPI schema returned from service does not have expected content."


def test_cache_existence(postgres_connection):
    """Test the cache existence."""
    if postgres_connection is None:
        pytest.skip("Postgres is not accessible.")

    value = read_conversation_history_count(postgres_connection)
    # check if history exists at all
    assert value is not None


@retry(max_attempts=3, wait_between_runs=10)
def test_valid_question_with_empty_attachment_list() -> None:
    """Check the REST API /v1/query with POST HTTP method using empty attachment list."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.ok
    ):
        response = client.post(
            endpoint,
            json={
                "conversation_id": "",
                "query": "what is kubernetes?",
                "attachments": [],
            },
            timeout=LLM_REST_API_TIMEOUT,
        )

        # HTTP OK should be returned
        assert response.status_code == requests.codes.ok

        response_utils.check_content_type(response, "application/json")


@retry(max_attempts=3, wait_between_runs=10)
def test_valid_question_with_one_attachment() -> None:
    """Check the REST API /v1/query with POST HTTP method using one attachment."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.ok
    ):
        response = client.post(
            endpoint,
            json={
                "conversation_id": "",
                "query": "what is kubernetes?",
                "attachments": [
                    {
                        "attachment_type": "log",
                        "content_type": "text/plain",
                        "content": "Kubernetes is a core component of OpenShift.",
                    },
                ],
            },
            timeout=LLM_REST_API_TIMEOUT,
        )

        # HTTP OK should be returned
        assert response.status_code == requests.codes.ok

        response_utils.check_content_type(response, "application/json")


@retry(max_attempts=3, wait_between_runs=10)
def test_valid_question_with_more_attachments() -> None:
    """Check the REST API /v1/query with POST HTTP method using two attachments."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.ok
    ):
        response = client.post(
            endpoint,
            json={
                "conversation_id": "",
                "query": "what is kubernetes?",
                "attachments": [
                    {
                        "attachment_type": "log",
                        "content_type": "text/plain",
                        "content": "Kubernetes is a core component of OpenShift.",
                    },
                    {
                        "attachment_type": "configuration",
                        "content_type": "application/json",
                        "content": "{'foo': 'bar'}",
                    },
                ],
            },
            timeout=LLM_REST_API_TIMEOUT,
        )

        # HTTP OK should be returned
        assert response.status_code == requests.codes.ok

        response_utils.check_content_type(response, "application/json")


@retry(max_attempts=3, wait_between_runs=10)
def test_valid_question_with_wrong_attachment_format_unknown_field() -> None:
    """Check the REST API /v1/query with POST HTTP method using attachment with wrong format."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.unprocessable_entity
    ):
        response = client.post(
            endpoint,
            json={
                "conversation_id": "",
                "query": "what is kubernetes?",
                "attachments": [
                    {
                        "xyzzy": "log",  # unknown field
                        "content_type": "text/plain",
                        "content": "this is attachment",
                    },
                ],
            },
            timeout=LLM_REST_API_TIMEOUT,
        )

        # the attachment should not be processed correctly
        assert response.status_code == requests.codes.unprocessable_entity

        json_response = response.json()
        details = json_response["detail"][0]
        assert details["msg"] == "Field required"
        assert details["type"] == "missing"


@retry(max_attempts=3, wait_between_runs=10)
def test_valid_question_with_wrong_attachment_format_missing_fields() -> None:
    """Check the REST API /v1/query with POST HTTP method using attachment with wrong format."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.unprocessable_entity
    ):
        response = client.post(
            endpoint,
            json={
                "conversation_id": "",
                "query": "what is kubernetes?",
                "attachments": [  # missing fields
                    {},
                ],
            },
            timeout=LLM_REST_API_TIMEOUT,
        )

        # the attachment should not be processed correctly
        assert response.status_code == requests.codes.unprocessable_entity

        json_response = response.json()
        details = json_response["detail"][0]
        assert details["msg"] == "Field required"
        assert details["type"] == "missing"


@retry(max_attempts=3, wait_between_runs=10)
def test_valid_question_with_wrong_attachment_format_field_of_different_type() -> None:
    """Check the REST API /v1/query with POST HTTP method using attachment with wrong value type."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.unprocessable_entity
    ):
        response = client.post(
            endpoint,
            json={
                "conversation_id": "",
                "query": "what is kubernetes?",
                "attachments": [
                    {
                        "attachment_type": 42,  # not a string
                        "content_type": "application/json",
                        "content": "{'foo': 'bar'}",
                    },
                ],
            },
            timeout=LLM_REST_API_TIMEOUT,
        )

        # the attachment should not be processed correctly
        assert response.status_code == requests.codes.unprocessable_entity

        json_response = response.json()
        details = json_response["detail"][0]
        assert details["msg"] == "Input should be a valid string"
        assert details["type"] == "string_type"


@retry(max_attempts=3, wait_between_runs=10)
def test_valid_question_with_wrong_attachment_format_unknown_attachment_type() -> None:
    """Check the REST API /v1/query with POST HTTP method using attachment with wrong type."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.unprocessable_entity
    ):
        response = client.post(
            endpoint,
            json={
                "conversation_id": "",
                "query": "what is kubernetes?",
                "attachments": [
                    {
                        "attachment_type": "unknown_type",
                        "content_type": "text/plain",
                        "content": "this is attachment",
                    },
                ],
            },
            timeout=LLM_REST_API_TIMEOUT,
        )

        # the attachment should not be processed correctly
        assert response.status_code == requests.codes.unprocessable_entity

        json_response = response.json()
        expected_response = {
            "detail": {
                "response": "Unable to process this request",
                "cause": "Attachment with improper type unknown_type detected",
            }
        }
        assert json_response == expected_response


@retry(max_attempts=3, wait_between_runs=10)
def test_valid_question_with_wrong_attachment_format_unknown_content_type() -> None:
    """Check the REST API /v1/query with POST HTTP method: attachment with wrong content type."""
    endpoint = "/v1/query"

    with metrics_utils.RestAPICallCounterChecker(
        metrics_client, endpoint, status_code=requests.codes.unprocessable_entity
    ):
        response = client.post(
            endpoint,
            json={
                "conversation_id": "",
                "query": "what is kubernetes?",
                "attachments": [
                    {
                        "attachment_type": "log",
                        "content_type": "unknown/type",
                        "content": "this is attachment",
                    },
                ],
            },
            timeout=LLM_REST_API_TIMEOUT,
        )

        # the attachment should not be processed correctly
        assert response.status_code == requests.codes.unprocessable_entity

        json_response = response.json()
        expected_response = {
            "detail": {
                "response": "Unable to process this request",
                "cause": "Attachment with improper content type unknown/type detected",
            }
        }
        assert json_response == expected_response


def test_conversation_in_postgres_cache(postgres_connection) -> None:
    """Check how/if the conversation is stored in cache."""
    if postgres_connection is None:
        pytest.skip("Postgres is not accessible.")

    cid = suid.get_suid()
    client_utils.perform_query(client, cid, "what is kubernetes?")

    conversation, updated_at = read_conversation_history(postgres_connection, cid)
    assert conversation is not None
    assert updated_at is not None

    # deserialize conversation into list of messages
    deserialized = json.loads(conversation)
    assert deserialized is not None

    # we expect one question + one answer
    assert len(deserialized) == 2

    # question check
    assert "what is kubernetes?" in deserialized[0].content

    # trivial check for answer (exact check is done in different tests)
    assert "Kubernetes" in deserialized[1].content

    # second question
    client_utils.perform_query(client, cid, "what is openshift virtualization?")

    conversation, updated_at = read_conversation_history(postgres_connection, cid)
    assert conversation is not None

    # unpickle conversation into list of messages
    deserialized = json.loads(conversation, errors="strict")
    assert deserialized is not None

    # we expect one question + one answer
    assert len(deserialized) == 4

    # first question
    assert "what is kubernetes?" in deserialized[0].content

    # first answer
    assert "Kubernetes" in deserialized[1].content

    # second question
    assert "what is openshift virtualization?" in deserialized[2].content

    # second answer
    assert "OpenShift" in deserialized[3].content


@pytest.mark.cluster()
def test_user_data_collection():
    """Test user data collection.

    It is performed by checking the user data collection container logs
    for the expected messages in logs.
    A bit of trick is required to check just the logs since the last
    action (as container logs can be influenced by other tests).
    """
    pod_name = None
    try:
        pod_name = cluster_utils.get_pod_by_prefix()[0]

        # enable collector script
        if pod_name is not None:
            cluster_utils.remove_file(pod_name, OLS_COLLECTOR_DISABLING_FILE)
            assert "disable_collector" not in cluster_utils.list_path(
                pod_name, OLS_USER_DATA_PATH
            )

        def filter_logs(logs: str, last_log_line: str) -> str:
            filtered_logs = []
            new_logs = False
            for line in logs.split("\n"):
                if line == last_log_line:
                    new_logs = True
                    continue
                if new_logs:
                    filtered_logs.append(line)
            return "\n".join(filtered_logs)

        def get_last_log_line(logs: str) -> str:
            return [line for line in logs.split("\n") if line][-1]

        # constants from tests/config/cluster_install/ols_manifests.yaml
        data_collection_container_name = "lightspeed-service-user-data-collector"
        pod_name = cluster_utils.get_pod_by_prefix()[0]

        # there are multiple tests running agains cluster, so user data
        # can be already present - we need to ensure the storage is empty
        # for this test
        user_data = cluster_utils.list_path(pod_name, OLS_USER_DATA_PATH)
        if user_data:
            cluster_utils.remove_dir(pod_name, OLS_USER_DATA_PATH + "/feedback")
            cluster_utils.remove_dir(pod_name, OLS_USER_DATA_PATH + "/transcripts")
            assert cluster_utils.list_path(pod_name, OLS_USER_DATA_PATH) == []

        # safety wait for the script to start after being disabled by other
        # tests
        time.sleep(OLS_USER_DATA_COLLECTION_INTERVAL + 5)

        # data shoud be pruned now and this is the point from which we want
        # to check the logs
        container_log = cluster_utils.get_container_log(
            pod_name, data_collection_container_name
        )
        last_log_line = get_last_log_line(container_log)

        # wait the collection period for some extra to give the script a
        # chance to log what we want to check
        time.sleep(OLS_USER_DATA_COLLECTION_INTERVAL + 1)

        # we just check that there are no data and the script is working
        container_log = cluster_utils.get_container_log(
            pod_name, data_collection_container_name
        )
        logs = filter_logs(container_log, last_log_line)

        assert "collected" not in logs
        assert "data uploaded with request_id:" not in logs
        assert "uploaded data removed" not in logs
        assert "data upload failed with response:" not in logs
        assert "contains no data, nothing to do..." in logs

        # get the log point for the next check
        last_log_line = get_last_log_line(container_log)

        # create a new data via feedback endpoint
        response = client.post(
            "/v1/feedback",
            json={
                "conversation_id": CONVERSATION_ID,
                "user_question": "what is OCP4?",
                "llm_response": "Openshift 4 is ...",
                "sentiment": 1,
            },
            timeout=BASIC_ENDPOINTS_TIMEOUT,
        )
        assert response.status_code == requests.codes.ok
        # ensure the script have enought time to send the payload before
        # we pull its logs
        time.sleep(OLS_USER_DATA_COLLECTION_INTERVAL + 5)

        # check that data was packaged, sent and removed
        container_log = cluster_utils.get_container_log(
            pod_name, data_collection_container_name
        )
        logs = filter_logs(container_log, last_log_line)
        assert "collected 1 files (splitted to 1 chunks) from" in logs
        assert "data uploaded with request_id:" in logs
        assert "uploaded data removed" in logs
        assert "data upload failed with response:" not in logs
        user_data = cluster_utils.list_path(pod_name, OLS_USER_DATA_PATH + "/feedback/")
        assert user_data == []

    finally:
        # disable collector script after test/on failure
        if pod_name is not None:
            cluster_utils.create_file(pod_name, OLS_COLLECTOR_DISABLING_FILE, "")


@pytest.mark.cluster()
def test_http_header_redaction():
    """Test that sensitive HTTP headers are redacted from the logs."""
    for header in HTTP_REQUEST_HEADERS_TO_REDACT:
        endpoint = "/liveness"
        with metrics_utils.RestAPICallCounterChecker(metrics_client, endpoint):
            response = client.get(
                endpoint,
                headers={f"{header}": "some_value"},
                timeout=BASIC_ENDPOINTS_TIMEOUT,
            )
            assert response.status_code == requests.codes.ok
            response_utils.check_content_type(response, "application/json")
            assert response.json() == {"alive": True}

    container_log = cluster_utils.get_container_log(
        cluster_utils.get_pod_by_prefix()[0], "lightspeed-service-api"
    )

    for header in HTTP_REQUEST_HEADERS_TO_REDACT:
        assert f'"{header}":"XXXXX"' in container_log
        assert f'"{header}":"some_value"' not in container_log


@pytest.mark.response_evaluation()
def test_model_response(request) -> None:
    """Evaluate model response."""
    args = Namespace(**vars(request.config.option))
    args.eval_provider_model_id = [f"{args.eval_provider}+{args.eval_model}"]
    args.eval_type = "consistency"

    val_success_flag = ResponseEvaluation(args, client).validate_response()
    # If flag is False, then response(s) is not consistent,
    # And score is more than cut-off score.
    # Please check eval_result/response_evaluation_* csv file in artifact folder or
    # Check the log to find out exact file path.
    assert val_success_flag


@pytest.mark.model_evaluation()
def test_model_evaluation(request) -> None:
    """Evaluate model."""
    # TODO: Use this to assert.
    ResponseEvaluation(request.config.option, client).evaluate_models()


@pytest.mark.azure_entra_id()
def test_azure_entra_id():
    """Test single question via Azure Entra ID credentials."""
    response = client.post(
        "/v1/query",
        json={
            "query": "what is kubernetes?",
            "provider": "azure_openai_with_entra_id",
            "model": "gpt-4o-mini",
        },
        timeout=LLM_REST_API_TIMEOUT,
    )
    assert response.status_code == requests.codes.ok

    response_utils.check_content_type(response, "application/json")
    print(vars(response))
    json_response = response.json()

    # checking a few major information from response
    assert "Kubernetes is" in json_response["response"]
    assert re.search(
        r"orchestration (tool|system|platform|engine)",
        json_response["response"],
        re.IGNORECASE,
    )
