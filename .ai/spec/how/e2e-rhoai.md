# RHOAI/vLLM E2E Provisioning Infrastructure

Implementation specification for the reusable RHOAI, KServe, and vLLM test assets under `tests/rhoai/`. The classic service test suite remains their owner; OLS-3472 adds model selection and a separate-checkout consumer for Agentic OLS product-e2e.

## Existing Flow

The RHOAI scripts wait for KServe CRDs and controller readiness, wait for a GPU node and allocatable GPU capacity, apply a vLLM `ServingRuntime`, and create an `InferenceService`. Classic service tests currently use a Llama model and model-specific tool-calling arguments.

## Planned Model-Parameterized Contract

1. [PLANNED: OLS-3472] The supported reusable entry point MUST be `tests/rhoai/scripts/provision-vllm.sh --profile <name> --output-env <path>`. It provisions only RHOAI/KServe/vLLM; it MUST NOT install OLS or invoke LSEval.
2. [PLANNED: OLS-3472] Checked-in profiles MUST live under `tests/rhoai/profiles/`. A profile MUST provide the model identifier and every model-sensitive vLLM setting needed by that profile, including tool-call parser and chat-template configuration when applicable. Common GPU, port, cache, credential, and KServe settings SHOULD remain shared.
3. [PLANNED: OLS-3472] Existing classic RHOAI tests MUST retain their current Llama behavior when they select the classic profile or use the documented backwards-compatible default.
4. [PLANNED: OLS-3472] The Agentic OLS consumer MUST select a Gemma 4 profile and MUST NOT rely on the classic Llama default.
5. [PLANNED: OLS-3472] The scripts MUST validate the requested profile and required profile values before applying Kubernetes resources. An unknown, incomplete, or explicitly requested but unavailable profile MUST fail; it MUST NOT silently fall back to Llama.
6. [PLANNED: OLS-3472] `RHOAI_VLLM_BASE_URL` is the OpenAI API root and MUST end in `/v1` with no trailing slash. Provisioning MUST wait for the `InferenceService`, then make an authenticated `GET ${RHOAI_VLLM_BASE_URL}/models` request and confirm that the response advertises the selected model identifier before returning success. Existing helpers that return the service root MUST append `/v1` when producing this output.
7. [PLANNED: OLS-3472] Provisioning MUST complete all remote downloads while cluster egress is available, including model weights, vLLM images, and any model-specific chat template. A profile MAY reference only a checked-in template after the restricted phase starts.
8. [PLANNED: OLS-3472] On success, `--output-env` MUST create a safely shell-quoted file containing non-secret `RHOAI_VLLM_BASE_URL`, `RHOAI_VLLM_MODEL`, `RHOAI_VLLM_NAMESPACE`, `RHOAI_VLLM_SERVICE_NAME`, `RHOAI_VLLM_SERVICE_PORT`, `RHOAI_VLLM_NETWORK_PORT`, and `RHOAI_VLLM_POD_SELECTOR_JSON`. The base URL uses the Service port; `RHOAI_VLLM_NETWORK_PORT` is the resolved backing-Pod target port used for NetworkPolicy enforcement. `RHOAI_VLLM_POD_SELECTOR_JSON` is compact JSON encoding a Kubernetes `metav1.LabelSelector` (`matchLabels` and/or `matchExpressions`), suitable for direct unmarshalling into a NetworkPolicy peer. Before returning, provisioning MUST verify that this selector's matched Pod set equals the set of backing Pods owned by the selected `InferenceService`—no backing Pod omitted and no unrelated Pod included. The file MUST NOT contain the API key or model-registry token.

Exact model repository names and image pullspecs are CI/release inputs rather than fixed behavioral constants. `RHOAI_VLLM_MODEL` MUST be the identifier returned by the vLLM models API, not merely the requested profile value. OLS-3472 uses the existing RawDeployment serving mode; other KServe modes require a future extension to this output contract.

## Separate-Checkout Consumer Contract

`lightspeed-agentic-operator` CI requires `LIGHTSPEED_SERVICE_REF` to match exactly 40 hexadecimal characters, verifies it resolves to a commit object, checks out `lightspeed-service` at that revision, verifies the resulting HEAD equals the supplied SHA, and invokes `tests/rhoai/scripts/provision-vllm.sh` from that checkout. The consumer supplies the Gemma 4 profile, `HUGGING_FACE_HUB_TOKEN`, and `VLLM_API_KEY`, then sources the non-secret output environment file before it enables its disconnected test boundary.

There is no Git submodule, copied script tree, runtime package dependency, or production dependency between the repositories. Changes that break the documented provisioning interface require coordinated spec and CI updates in both repositories.

## Failure Diagnostics

On failure, the provisioning path SHOULD collect or identify:

- GPU node labels, capacity, and allocatable resources
- RHOAI/KServe CRD and controller readiness
- `ServingRuntime` and `InferenceService` status
- Kubernetes events and vLLM Pod status/logs
- The selected profile name and non-secret model-serving parameters

Credentials, API keys, and model-registry tokens MUST NOT be printed.

## Out of Scope

- Running Agentic OLS product-e2e tests from this repository
- Defining the disconnected egress boundary
- LSEval, LLM judges, or agentic output-quality scoring
- Moving the RHOAI assets into a shared repository or Git submodule
