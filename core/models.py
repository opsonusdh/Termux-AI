PROVIDERS: dict[str, dict] = {
    "google": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1/",
    },
}


MODEL_SLOTS: list[dict] = [
    # Google models
    {"provider_id": "google", "name": "gemini-3.7-flash",                   "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.6-flash",                   "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.5-flash",                   "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.1-flash-lite",              "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.1-flash-lite-preview",      "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3-flash-preview",             "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.1-pro-preview",             "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.1-pro-preview-customtools", "max_tokens": None},
    {"provider_id": "google", "name": "gemini-flash-latest",                "max_tokens": None},
    {"provider_id": "google", "name": "gemini-flash-lite-latest",           "max_tokens": None},
    {"provider_id": "google", "name": "gemini-pro-latest",                  "max_tokens": None},
    {"provider_id": "google", "name": "gemma-4-31b-it",                     "max_tokens": None},
    {"provider_id": "google", "name": "gemma-4-26b-a4b-it",                 "max_tokens": None},

    # Groq & Nvidia models
    {"provider_id": "groq",       "name": "deepseek-r1-distill-llama-70b", "max_tokens": 4096},
    {"provider_id": "groq",       "name": "qwen-2.5-32b",                  "max_tokens": 4096},
    {"provider_id": "groq",       "name": "openai/gpt-oss-120b",           "max_tokens": 4096},
    {"provider_id": "nvidia",     "name": "deepseek-ai/deepseek-r1",       "max_tokens": 4096},
    {"provider_id": "nvidia",     "name": "nvidia/llama-3.1-nemotron-70b-instruct", "max_tokens": 4096},
    # Nemotron 3 family, direct via NVIDIA's own API (not OpenRouter) — these
    # gate output on extra_body.chat_template_kwargs.enable_thinking,
    # per build.nvidia.com's own docs for each model; max_tokens matches NVIDIA's
    # documented example for this family.
    {"provider_id": "nvidia", "name": "nvidia/nemotron-3-ultra-550b-a55b",     "max_tokens": 16384, "enable_thinking": True},
    {"provider_id": "nvidia", "name": "nvidia/nemotron-3-super-120b-a12b",     "max_tokens": 16384, "enable_thinking": True},
    {"provider_id": "nvidia", "name": "nvidia/nemotron-3.5-lightning-30b-a3b", "max_tokens": 16384, "enable_thinking": True},
    # Poolside's own build.nvidia.com page shows a plain non-streaming call with
    # no extra_body — no enable_thinking flag for this one.
    {"provider_id": "nvidia", "name": "poolside/laguna-xs-2.1",               "max_tokens": 8192},

    # OpenRouter Top-Tier Reasoning & Coding Models (Ranked Smartest First)
    {"provider_id": "openrouter", "name": "google/gemma-4-31b-it:free",                   "max_tokens": None},
    {"provider_id": "openrouter", "name": "z-ai/glm-5.2:free",                             "max_tokens": None},
    {"provider_id": "openrouter", "name": "minimax/minimax-m3:free",                       "max_tokens": None},
    {"provider_id": "openrouter", "name": "thinkingmachines/inkling:free",                 "max_tokens": None},
    {"provider_id": "openrouter", "name": "poolside/laguna-s-2.1:free",                    "max_tokens": None},
    {"provider_id": "openrouter", "name": "cohere/north-mini-code:free",                  "max_tokens": None},
]


AGENT_MODEL_SLOTS: list[dict] = [
    {"provider_id": "nvidia", "name": "nvidia/nemotron-3-ultra-550b-a55b",     "max_tokens": 16384, "enable_thinking": True},
    {"provider_id": "nvidia", "name": "nvidia/nemotron-3.5-lightning-30b-a3b", "max_tokens": 16384, "enable_thinking": True},
    {"provider_id": "nvidia", "name": "poolside/laguna-xs-2.1",               "max_tokens": 8192},
    {"provider_id": "openrouter", "name": "cohere/north-mini-code:free",           "max_tokens": None},
    {"provider_id": "groq",       "name": "openai/gpt-oss-120b",                   "max_tokens": 4096},
    {"provider_id": "groq",       "name": "qwen/qwen3.8-27b",                    "max_tokens": 4096},
]
