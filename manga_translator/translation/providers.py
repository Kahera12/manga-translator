"""Provider registry: every supported translation backend + live model discovery.

Two families of providers:

* **llm** – chat/completion models that receive the whole page as JSON and return
  structured JSON (page context, glossary, tone). Gemini, OpenAI, DeepSeek,
  Anthropic, OpenRouter, Groq, Mistral, xAI, Ollama, LM Studio, ...
* **mt** – classic machine-translation APIs that receive the page as a batch of
  texts (Google Cloud Translation, DeepL, Yandex, Microsoft, LibreTranslate,
  MyMemory). No page context, but they are cheap, fast and need no prompt.

`list_models()` / `list_languages()` query the provider with the configured API
key so the settings dialog shows the models that key can actually use, with a
curated static list as a fallback when the request fails or no key is set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

from ..core.logging_setup import get_logger

log = get_logger("providers")

GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"

# --- language codes used by the machine-translation providers ---------------
ISO_LANGUAGES: List[str] = [
    "ru", "en", "zh", "zh-Hans", "zh-Hant", "ja", "ko", "de", "fr", "es", "it",
    "pt", "pl", "uk", "tr", "ar", "hi", "th", "vi", "id", "nl", "sv", "cs", "he",
]


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    kind: str                      # llm-gemini | llm-openai | llm-anthropic | mt | fake
    service: str = ""              # mt service key (google, deepl, yandex, ...)
    base_url: str = ""
    key_env: Tuple[str, ...] = ()
    key_label: str = "API key"
    key_help: str = ""
    needs_key: bool = True
    default_model: str = ""
    static_models: Tuple[str, ...] = ()
    supports_model_list: bool = False
    supports_language_list: bool = False
    json_mode: str = "object"      # schema | object | none (structured-output support)
    local: bool = False            # runs on this machine (no cloud key needed)
    note: str = ""
    extra_headers: Dict[str, str] = field(default_factory=dict)

    @property
    def is_llm(self) -> bool:
        return self.kind.startswith("llm")

    @property
    def unit(self) -> str:
        return "model" if self.is_llm else "language"


def _spec(**kwargs) -> ProviderSpec:
    return ProviderSpec(**kwargs)


OPENAI_STATIC = ("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1", "o4-mini")

PROVIDERS: Dict[str, ProviderSpec] = {
    # --- Google ---------------------------------------------------------
    "gemini": _spec(
        id="gemini",
        label="Google Gemini (generativelanguage)",
        kind="llm-gemini",
        base_url=GEMINI_BASE_URL,
        key_env=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        key_label="Gemini API key",
        key_help="aistudio.google.com → Get API key",
        default_model=GEMINI_DEFAULT_MODEL,
        static_models=(
            GEMINI_DEFAULT_MODEL, "gemini-2.5-flash", "gemini-2.5-pro",
            "gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro",
        ),
        supports_model_list=True,
        note="page-level JSON translation with responseSchema",
    ),
    "google-translate": _spec(
        id="google-translate",
        label="Google Cloud Translation v2",
        kind="mt",
        service="google",
        base_url="https://translation.googleapis.com",
        key_env=("GOOGLE_TRANSLATE_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"),
        key_label="Google Cloud API key",
        key_help="console.cloud.google.com → APIs → Cloud Translation API → key",
        default_model="nmt",
        static_models=("nmt", "base"),
        supports_language_list=True,
        note="classic MT, one request per page (batch of texts)",
    ),
    # --- OpenAI-compatible clouds ---------------------------------------
    "openai": _spec(
        id="openai", label="OpenAI", kind="llm-openai",
        base_url="https://api.openai.com/v1", key_env=("OPENAI_API_KEY",),
        key_label="OpenAI API key", key_help="platform.openai.com/api-keys",
        default_model="gpt-4o-mini", static_models=OPENAI_STATIC, supports_model_list=True,
        json_mode="schema",
    ),
    "deepseek": _spec(
        id="deepseek", label="DeepSeek", kind="llm-openai",
        base_url="https://api.deepseek.com/v1", key_env=("DEEPSEEK_API_KEY",),
        key_label="DeepSeek API key", key_help="platform.deepseek.com/api_keys",
        default_model="deepseek-chat",
        static_models=("deepseek-chat", "deepseek-reasoner"), supports_model_list=True,
        note="cheap, strong on Chinese → Russian",
    ),
    "openrouter": _spec(
        id="openrouter", label="OpenRouter (aggregator)", kind="llm-openai",
        base_url="https://openrouter.ai/api/v1", key_env=("OPENROUTER_API_KEY",),
        key_label="OpenRouter API key", key_help="openrouter.ai/keys",
        default_model="google/gemini-2.0-flash-001",
        static_models=(
            "google/gemini-2.0-flash-001", "deepseek/deepseek-chat",
            "qwen/qwen-2.5-72b-instruct", "anthropic/claude-3.5-sonnet",
        ),
        supports_model_list=True, note="hundreds of models behind one key",
    ),
    "anthropic": _spec(
        id="anthropic", label="Anthropic Claude", kind="llm-anthropic",
        base_url="https://api.anthropic.com", key_env=("ANTHROPIC_API_KEY",),
        key_label="Anthropic API key", key_help="console.anthropic.com",
        default_model="claude-3-5-haiku-latest",
        static_models=("claude-3-5-haiku-latest", "claude-3-5-sonnet-latest",
                       "claude-3-7-sonnet-latest", "claude-sonnet-4-5"),
        supports_model_list=True, json_mode="none",
    ),
    "mistral": _spec(
        id="mistral", label="Mistral AI", kind="llm-openai",
        base_url="https://api.mistral.ai/v1", key_env=("MISTRAL_API_KEY",),
        default_model="mistral-small-latest",
        static_models=("mistral-small-latest", "mistral-large-latest",
                       "open-mistral-nemo", "ministral-8b-latest"),
        supports_model_list=True,
    ),
    "groq": _spec(
        id="groq", label="Groq (fast inference)", kind="llm-openai",
        base_url="https://api.groq.com/openai/v1", key_env=("GROQ_API_KEY",),
        default_model="llama-3.3-70b-versatile",
        static_models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant",
                       "qwen/qwen3-32b", "moonshotai/kimi-k2-instruct"),
        supports_model_list=True, note="very low latency",
    ),
    "together": _spec(
        id="together", label="Together AI", kind="llm-openai",
        base_url="https://api.together.xyz/v1", key_env=("TOGETHER_API_KEY",),
        default_model="Qwen/Qwen2.5-72B-Instruct-Turbo",
        static_models=("Qwen/Qwen2.5-72B-Instruct-Turbo", "deepseek-ai/DeepSeek-V3",
                       "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
        supports_model_list=True,
    ),
    "fireworks": _spec(
        id="fireworks", label="Fireworks AI", kind="llm-openai",
        base_url="https://api.fireworks.ai/inference/v1", key_env=("FIREWORKS_API_KEY",),
        default_model="accounts/fireworks/models/llama-v3p3-70b-instruct",
        static_models=("accounts/fireworks/models/llama-v3p3-70b-instruct",
                       "accounts/fireworks/models/qwen2p5-72b-instruct"),
        supports_model_list=True,
    ),
    "deepinfra": _spec(
        id="deepinfra", label="DeepInfra", kind="llm-openai",
        base_url="https://api.deepinfra.com/v1/openai", key_env=("DEEPINFRA_API_KEY",),
        default_model="Qwen/Qwen2.5-72B-Instruct",
        static_models=("Qwen/Qwen2.5-72B-Instruct", "meta-llama/Llama-3.3-70B-Instruct"),
        supports_model_list=True,
    ),
    "cerebras": _spec(
        id="cerebras", label="Cerebras", kind="llm-openai",
        base_url="https://api.cerebras.ai/v1", key_env=("CEREBRAS_API_KEY",),
        default_model="llama-3.3-70b",
        static_models=("llama-3.3-70b", "llama3.1-8b", "qwen-3-32b"),
        supports_model_list=True, json_mode="schema",
    ),
    "xai": _spec(
        id="xai", label="xAI Grok", kind="llm-openai",
        base_url="https://api.x.ai/v1", key_env=("XAI_API_KEY", "GROK_API_KEY"),
        default_model="grok-2-latest",
        static_models=("grok-2-latest", "grok-2-1212", "grok-beta"),
        supports_model_list=True,
    ),
    "perplexity": _spec(
        id="perplexity", label="Perplexity", kind="llm-openai",
        base_url="https://api.perplexity.ai", key_env=("PERPLEXITY_API_KEY",),
        default_model="sonar",
        static_models=("sonar", "sonar-pro", "sonar-reasoning"),
        supports_model_list=True, json_mode="none",
    ),
    "nvidia": _spec(
        id="nvidia", label="NVIDIA NIM", kind="llm-openai",
        base_url="https://integrate.api.nvidia.com/v1", key_env=("NVIDIA_API_KEY",),
        default_model="meta/llama-3.3-70b-instruct",
        static_models=("meta/llama-3.3-70b-instruct", "qwen/qwen2.5-72b-instruct"),
        supports_model_list=True,
    ),
    "sambanova": _spec(
        id="sambanova", label="SambaNova", kind="llm-openai",
        base_url="https://api.sambanova.ai/v1", key_env=("SAMBANOVA_API_KEY",),
        default_model="Meta-Llama-3.3-70B-Instruct",
        static_models=("Meta-Llama-3.3-70B-Instruct", "Qwen2.5-72B-Instruct"),
        supports_model_list=True,
    ),
    "github-models": _spec(
        id="github-models", label="GitHub Models", kind="llm-openai",
        base_url="https://models.inference.ai.azure.com", key_env=("GITHUB_TOKEN", "GITHUB_MODELS_TOKEN"),
        key_label="GitHub token", key_help="github.com → Settings → Developer settings → PAT (models:read)",
        default_model="gpt-4o-mini",
        static_models=("gpt-4o-mini", "gpt-4o", "Phi-3.5-MoE-instruct"),
        supports_model_list=True,
    ),
    "moonshot": _spec(
        id="moonshot", label="Moonshot Kimi", kind="llm-openai",
        base_url="https://api.moonshot.cn/v1", key_env=("MOONSHOT_API_KEY",),
        default_model="moonshot-v1-8k",
        static_models=("moonshot-v1-8k", "moonshot-v1-32k", "kimi-k2-0711-preview"),
        supports_model_list=True,
    ),
    "zhipu": _spec(
        id="zhipu", label="Zhipu GLM", kind="llm-openai",
        base_url="https://open.bigmodel.cn/api/paas/v4", key_env=("ZHIPU_API_KEY", "GLM_API_KEY"),
        default_model="glm-4-flash",
        static_models=("glm-4-flash", "glm-4-air", "glm-4-plus"),
        supports_model_list=True,
    ),
    "qwen": _spec(
        id="qwen", label="Alibaba Qwen (DashScope)", kind="llm-openai",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        key_env=("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
        default_model="qwen-plus",
        static_models=("qwen-plus", "qwen-max", "qwen-turbo", "qwen2.5-72b-instruct"),
        supports_model_list=True,
    ),
    "siliconflow": _spec(
        id="siliconflow", label="SiliconFlow", kind="llm-openai",
        base_url="https://api.siliconflow.cn/v1", key_env=("SILICONFLOW_API_KEY",),
        default_model="Qwen/Qwen2.5-72B-Instruct",
        static_models=("Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-V3"),
        supports_model_list=True,
    ),
    "azure-openai": _spec(
        id="azure-openai", label="Azure OpenAI", kind="llm-openai",
        base_url="https://YOUR-RESOURCE.openai.azure.com/openai/deployments/YOUR-DEPLOYMENT",
        key_env=("AZURE_OPENAI_API_KEY",),
        key_label="Azure OpenAI key",
        key_help="endpoint + deployment name go into the base URL; a deployment "
                 "name is the 'model' value",
        default_model="gpt-4o-mini",
        static_models=("gpt-4o-mini", "gpt-4o", "gpt-35-turbo"),
        supports_model_list=False, json_mode="schema",
        note="requires ?api-version=… (add it to the base URL if your gateway needs it)",
    ),
    # --- local servers ---------------------------------------------------
    "ollama": _spec(
        id="ollama", label="Ollama (local)", kind="llm-openai",
        base_url="http://localhost:11434/v1", key_env=("OLLAMA_API_KEY",),
        key_label="Ollama key (any value if not set)",
        needs_key=False, local=True,
        default_model="qwen2.5:7b",
        static_models=("qwen2.5:7b", "qwen2.5:14b", "llama3.1:8b", "gemma2:9b"),
        supports_model_list=True, note="offline, free, no API key",
    ),
    "lmstudio": _spec(
        id="lmstudio", label="LM Studio (local)", kind="llm-openai",
        base_url="http://localhost:1234/v1", key_env=("LMSTUDIO_API_KEY",),
        needs_key=False, local=True, default_model="local-model",
        static_models=("local-model",), supports_model_list=True, json_mode="schema",
        note="start LM Studio's local server first",
    ),
    "llamacpp": _spec(
        id="llamacpp", label="llama.cpp server (local)", kind="llm-openai",
        base_url="http://localhost:8080/v1", needs_key=False, local=True,
        default_model="local-model", static_models=("local-model",),
        supports_model_list=True, json_mode="schema",
    ),
    "vllm": _spec(
        id="vllm", label="vLLM / LocalAI (local)", kind="llm-openai",
        base_url="http://localhost:8000/v1", needs_key=False, local=True,
        default_model="local-model", static_models=("local-model",),
        supports_model_list=True, json_mode="schema",
    ),
    # --- classic MT APIs -------------------------------------------------
    "deepl": _spec(
        id="deepl", label="DeepL", kind="mt", service="deepl",
        base_url="https://api-free.deepl.com", key_env=("DEEPL_API_KEY", "DEEPL_AUTH_KEY"),
        key_label="DeepL auth key", key_help="deepl.com/pro-api (keys ending in :fx are free tier)",
        default_model="deepl", static_models=("deepl", "deepl-pro"),
        supports_language_list=True, note="excellent Russian, one request per page",
    ),
    "yandex": _spec(
        id="yandex", label="Yandex Translate", kind="mt", service="yandex",
        base_url="https://translate.api.cloud.yandex.net", key_env=("YANDEX_API_KEY", "YC_API_KEY"),
        key_label="Yandex Cloud API key", key_help="Yandex Cloud → service account → API key",
        default_model="yandex", static_models=("yandex",),
        supports_language_list=True, note="good for Russian",
    ),
    "microsoft": _spec(
        id="microsoft", label="Microsoft Translator (Azure)", kind="mt", service="microsoft",
        base_url="https://api.cognitive.microsofttranslator.com",
        key_env=("AZURE_TRANSLATOR_KEY", "MICROSOFT_TRANSLATOR_KEY"),
        key_label="Azure Translator key",
        key_help="portal.azure.com → Translator resource → Keys and endpoint",
        default_model="microsoft", static_models=("microsoft",),
        supports_language_list=True,
    ),
    "libretranslate": _spec(
        id="libretranslate", label="LibreTranslate (self-hosted)", kind="mt", service="libretranslate",
        base_url="http://localhost:5000", key_env=("LIBRETRANSLATE_API_KEY",),
        needs_key=False, local=True, default_model="libretranslate",
        static_models=("libretranslate",), supports_language_list=True,
        note="docker run -p 5000:5000 libretranslate/libretranslate",
    ),
    "mymemory": _spec(
        id="mymemory", label="MyMemory (free, no key)", kind="mt", service="mymemory",
        base_url="https://api.mymemory.translated.net", needs_key=False,
        default_model="mymemory", static_models=("mymemory",),
        note="works without a key (anonymous daily quota), good for a smoke test",
    ),
    # --- offline ---------------------------------------------------------
    "fake": _spec(
        id="fake", label="Offline stub (testing)", kind="fake",
        needs_key=False, local=True, default_model="fake-model",
        static_models=("fake-model",), note="no network; structure/overlay testing only",
    ),
}


def get_spec(provider: str) -> Optional[ProviderSpec]:
    return PROVIDERS.get((provider or "").strip().lower())


def provider_ids() -> List[str]:
    return list(PROVIDERS)


def provider_choices() -> List[Tuple[str, str]]:
    """[(id, label)] sorted for a combo box: LLMs, MT, local, offline."""
    order = {"llm-gemini": 0, "llm-openai": 1, "llm-anthropic": 2, "mt": 3, "fake": 4}
    specs = sorted(PROVIDERS.values(), key=lambda s: (order.get(s.kind, 9), not s.local, s.label))
    return [(spec.id, spec.label) for spec in specs]


def default_base_url(provider: str) -> str:
    spec = get_spec(provider)
    return spec.base_url if spec else ""


def default_model(provider: str) -> str:
    spec = get_spec(provider)
    return spec.default_model if spec else ""


def resolve_base_url(provider: str, configured: str = "") -> str:
    """Effective endpoint for a provider.

    A single ``base_url`` is stored in the config, so switching the provider
    (or setting ``TRANSLATION_PROVIDER`` in the environment) must not leave the
    previous provider's endpoint behind: a value that equals *another* provider's
    default is treated as "not configured" and replaced by this provider's
    default. Custom endpoints are always kept.
    """
    spec = get_spec(provider)
    default = (spec.base_url if spec else "").rstrip("/")
    value = (configured or "").strip().rstrip("/")
    if not value:
        return default
    if value == default:
        return default
    known_defaults = {s.base_url.rstrip("/") for s in PROVIDERS.values() if s.base_url}
    if value in known_defaults:
        return default
    return value


# --- live discovery --------------------------------------------------------

MODEL_LIST_TIMEOUT_S = 20.0


def _client(timeout: float = MODEL_LIST_TIMEOUT_S) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True)


def list_models(
    provider: str,
    base_url: str = "",
    api_key: str = "",
    timeout: float = MODEL_LIST_TIMEOUT_S,
) -> Tuple[List[str], str, str]:
    """Query the provider for the models/languages this key can use.

    Returns ``(items, kind, error)`` where kind is ``"model"`` or ``"language"``
    and error is an empty string on success. Never raises: the settings dialog
    falls back to the curated static list.
    """
    spec = get_spec(provider)
    if spec is None:
        return [], "model", f"unknown provider '{provider}'"
    base = resolve_base_url(provider, base_url) if base_url else spec.base_url.rstrip("/")
    if not spec.is_llm and not spec.supports_language_list and spec.kind == "mt":
        return list(spec.static_models), "model", ""
    if spec.kind == "fake":
        return list(spec.static_models), "model", ""
    if spec.needs_key and not api_key:
        return list(spec.static_models), spec.unit, "no API key set"

    try:
        if spec.kind == "llm-gemini":
            return _gemini_models(base, api_key, timeout)
        if spec.kind == "llm-openai":
            if spec.id == "ollama":
                items, error = _ollama_models(base, timeout)
                if items:
                    return items, "model", ""
            return _openai_models(base, api_key, timeout)
        if spec.kind == "llm-anthropic":
            return _anthropic_models(base, api_key, timeout)
        return _mt_languages(spec, base, api_key, timeout)
    except httpx.HTTPError as exc:
        return list(spec.static_models), spec.unit, f"{exc.__class__.__name__}"
    except Exception as exc:  # pragma: no cover - defensive
        return list(spec.static_models), spec.unit, f"{exc.__class__.__name__}: {exc}"


def _gemini_models(base: str, api_key: str, timeout: float) -> Tuple[List[str], str, str]:
    spec = PROVIDERS["gemini"]
    url = f"{base}/v1beta/models"
    params = {"key": api_key} if api_key else None
    with _client(timeout) as client:
        response = client.get(url, params=params)
    if response.status_code != 200:
        return list(spec.static_models), "model", _short_error(response)
    payload = response.json()
    models = []
    for item in payload.get("models", []):
        methods = item.get("supportedGenerationMethods") or []
        if methods and "generateContent" not in methods:
            continue
        name = str(item.get("name", "")).removeprefix("models/")
        if name and "embedding" not in name and "aqa" not in name:
            models.append(name)
    if not models:
        return list(spec.static_models), "model", "no generateContent models for this key"
    return sorted(models), "model", ""


def _openai_models(base: str, api_key: str, timeout: float) -> Tuple[List[str], str, str]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with _client(timeout) as client:
        response = client.get(f"{base}/models", headers=headers)
    if response.status_code != 200:
        return [], "model", _short_error(response)
    payload = response.json()
    items = payload.get("data") if isinstance(payload, dict) else payload
    models = []
    for item in items or []:
        if isinstance(item, dict):
            name = item.get("id") or item.get("name") or item.get("model")
        else:
            name = str(item)
        if name:
            models.append(str(name))
    return sorted(set(models)), "model", ""


def _ollama_models(base: str, timeout: float) -> Tuple[List[str], str]:
    root = base.replace("/v1", "")
    with _client(timeout) as client:
        response = client.get(f"{root}/api/tags")
    if response.status_code != 200:
        return [], _short_error(response)
    payload = response.json()
    return sorted({str(m.get("name")) for m in payload.get("models", []) if m.get("name")}), ""


def _anthropic_models(base: str, api_key: str, timeout: float) -> Tuple[List[str], str, str]:
    spec = PROVIDERS["anthropic"]
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    with _client(timeout) as client:
        response = client.get(f"{base}/v1/models", headers=headers, params={"limit": 100})
    if response.status_code != 200:
        return list(spec.static_models), "model", _short_error(response)
    payload = response.json()
    models = [str(item.get("id")) for item in payload.get("data", []) if item.get("id")]
    return sorted(models) or list(spec.static_models), "model", ""


def _mt_languages(spec: ProviderSpec, base: str, api_key: str,
                  timeout: float) -> Tuple[List[str], str, str]:
    """Target languages offered by a classic MT provider."""
    service = spec.service
    with _client(timeout) as client:
        if service == "google":
            response = client.get(f"{base}/language/translate/v2/languages",
                                  params={"key": api_key, "target": "ru"})
            if response.status_code != 200:
                return list(ISO_LANGUAGES), "language", _short_error(response)
            data = response.json().get("data", {}).get("languages", [])
            return sorted({item.get("language") for item in data if item.get("language")}), "language", ""
        if service == "deepl":
            response = client.get(f"{base}/v2/languages",
                                  headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
                                  params={"type": "target"})
            if response.status_code != 200:
                return list(ISO_LANGUAGES), "language", _short_error(response)
            data = response.json()
            codes = sorted({item.get("language") for item in data if item.get("language")})
            return codes, "language", ""
        if service == "microsoft":
            response = client.get(f"{base}/languages", params={"api-version": "3.0", "scope": "translation"})
            if response.status_code != 200:
                return list(ISO_LANGUAGES), "language", _short_error(response)
            data = response.json().get("translation", {})
            return sorted(data.keys()), "language", ""
        if service == "libretranslate":
            response = client.get(f"{base}/languages")
            if response.status_code != 200:
                return list(ISO_LANGUAGES), "language", _short_error(response)
            data = response.json()
            return sorted({str(item.get("code")) for item in data if item.get("code")}), "language", ""
        if service == "yandex":
            headers = {"Authorization": f"Api-Key {api_key}"}
            response = client.post(f"{base}/translate/v2/languages", headers=headers, json={})
            if response.status_code != 200:
                return list(ISO_LANGUAGES), "language", _short_error(response)
            data = response.json().get("languages", [])
            return sorted({str(item.get("code")) for item in data if item.get("code")}), "language", ""
    return list(ISO_LANGUAGES), "language", ""


def _short_error(response: httpx.Response) -> str:
    text = (response.text or "").strip().replace("\n", " ")
    return f"HTTP {response.status_code}: {text[:160]}"


def static_models(provider: str) -> List[str]:
    spec = get_spec(provider)
    return list(spec.static_models) if spec else []
