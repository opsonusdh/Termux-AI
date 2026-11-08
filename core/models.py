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
    # NVIDIA Visual Design endpoints — image generation (FLUX family).
    # Image inference is NOT served from integrate.api.nvidia.com; it lives on
    # its own host under /v1/genai/<model>.
    "nvidia-images": {
        "base_url": "https://ai.api.nvidia.com/v1",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
    },
    # Ollama local server — free (one-time download), no API key required.
    "ollama-local": {
        "base_url": "http://127.0.0.1:11434/v1/",
    },
    # Ollama Cloud — hosted OpenAI-compatible API.  Only the models of the
    # account's Free plan are used (see OLLAMA_FREE_PLAN_MODELS); every other
    # cloud model is paid and refuses with HTTP 402.  Requires an API key from
    # the Ollama Cloud dashboard.  Cloud models use the `-cloud` suffix.
    "ollama": {
        "base_url": "https://ollama.com/v1",
    },
}


# ── FREE-ONLY MODEL SLOTS (main chat rotation) ─────────────────────────────────
# Termux-AI is built for free AI.  Every slot below is served by a free plan,
# a free tier or a zero-priced endpoint; nothing here can ask the user for a
# credit card.  The paid routes were removed on 2026-10-30 after they were
# verified live to fail with HTTP 402 ("This request requires more credits") —
# they only burned a rotation slot on every request.
#
# The evidence per provider (re-verified live on 2026-10-31):
#
#   * Ollama Cloud — the account has a "Free plan".  A model outside that plan
#     answers HTTP 402 "this model is not included in your free usage", so
#     the routes outside the plan were identified as paid and dropped.  Only
#     the models of the Free plan (OLLAMA_FREE_PLAN_MODELS) are routable.
#   * Google — the Gemini Developer API *free tier*.  The service names the
#     quota metric itself: "generate_content_free_tier_requests", quota id
#     "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", limit 5 per
#     minute per model per project.  The Pro models (gemini-3.1-pro-preview,
#     gemini-pro-latest, gemini-4-argon) report a free-tier quota of **0** —
#     they are paid-only and are gone from this list.
#   * Groq — the free plan.  Groq's own docs say the x-ratelimit-limit-requests
#     header always means requests *per day*; the account reports 1000 RPD and
#     8000 TPM, which are exactly the published Free Plan limits for
#     gpt-oss-120b / gpt-oss-20b / qwen3.8-27b.  Four of the five configured
#     Groq keys belong to restricted organizations; only one works.
#   * NVIDIA — the free serverless API (build.nvidia.com).  Every model here
#     answers without a credit balance.
#   * OpenRouter — only the zero-priced ":free" variants.  The live catalogue
#     prices them at 0/0; every paid slug answered 402 with a $0.00 balance.
#
MODEL_SLOTS: list[dict] = [
    {"provider_id": "ollama", "name": "beam:cloud",     "max_tokens": None, "reasoning_effort": "high"},
    {"provider_id": "ollama", "name": "nemotron-3-ultra:cloud",     "max_tokens": None, "reasoning_effort": "high"},
    {"provider_id": "ollama", "name": "gpt-oss:120b-cloud",         "max_tokens": None, "reasoning_effort": "high"},
    # Google — Gemini free tier (5 requests/minute/model/project).
    {"provider_id": "google", "name": "gemini-flash-latest",               "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.8-flash",               "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.7-flash",               "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.6-flash",               "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.5-flash",               "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.1-flash-lite",          "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.1-flash-lite-preview",  "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3-flash-preview",         "max_tokens": None},
    {"provider_id": "google", "name": "gemini-flash-latest",            "max_tokens": None},
    {"provider_id": "google", "name": "gemini-flash-lite-latest",       "max_tokens": None},
    {"provider_id": "google", "name": "gemini-2.5-flash",               "max_tokens": None},
    {"provider_id": "google", "name": "gemini-2.5-flash-lite",          "max_tokens": None},
    {"provider_id": "google", "name": "gemma-4-31b-it",                 "max_tokens": None},
    {"provider_id": "google", "name": "gemma-4-26b-a4b-it",             "max_tokens": None},
    # Groq — free plan (1000 requests/day, 8000 tokens/minute).
    {"provider_id": "groq",       "name": "openai/gpt-oss-120b",         "max_tokens": 4096},
    # NVIDIA — free serverless API.  The Nemotron family gates its output on
    # extra_body.chat_template_kwargs.enable_thinking, per build.nvidia.com's
    # own docs for each model; max_tokens matches NVIDIA's documented example
    # for this family.
    {"provider_id": "nvidia", "name": "nvidia/nemotron-3-ultra-550b-a55b",     "max_tokens": 16384, "enable_thinking": True},
    {"provider_id": "nvidia", "name": "minimaxai/minimax-m3.1-flash",               "max_tokens": 16384, "enable_thinking": True},
    {"provider_id": "nvidia", "name": "nvidia/nemotron-3.5-super-vl-120b-a12b",     "max_tokens": 16384, "enable_thinking": True},
    {"provider_id": "nvidia", "name": "nvidia/nemotron-3.5-lightning-30b-a3b", "max_tokens": 16384, "enable_thinking": True},
    # OpenRouter — only the zero-priced ":free" variants are used here.
    {"provider_id": "openrouter", "name": "nvidia/nemotron-3-ultra-550b-a55b:free", "max_tokens": None},
    {"provider_id": "openrouter", "name": "cohere/north-mini-code:free",            "max_tokens": None},
]


# ── FREE-ONLY AGENT SLOTS (autonomous agent rotation) ──────────────────────────
# Same policy as MODEL_SLOTS: a free plan, a free tier or a zero-priced
# endpoint — never a paid slug.  The removed routes were verified live to
# refuse with HTTP 402 (OpenRouter: "$0.00 balance"; Ollama: "not included in
# your free usage") or to be unavailable (NVIDIA answers HTTP 404 "Function
# ... Not found for account" for nemotron-3-super-120b-a12b, nemotron-4-340b-instruct
# and kimi-k2.6, and deepseek-v4.1-flash needs more than 300 s for its first
# token on the free endpoint).
AGENT_MODEL_SLOTS: list[dict] = [
    # Kimi K3.1 Pro is NOT registered here, because it cannot be served by
    # this account.  Verified live on 2026-11-06 against
    # integrate.api.nvidia.com/v1:
    #   * the live catalogue (84 ids) contains no Kimi K3.1 at all — only
    #     moonshotai/kimi-k2.6;
    #   * moonshotai/kimi-k3.1-pro and moonshotai/kimi-k3.1 both answer
    #     HTTP 404 "404 page not found";
    #   * moonshotai/kimi-k2.6 answers HTTP 404 "Function ... Not found for
    #     account", i.e. the account has no Moonshot function enabled;
    #   * on OpenRouter the Kimi K3 / K3.1 Pro slugs are paid and were
    #     removed from this repo in 2026-10 after they refused with HTTP 402
    #     ("this model is not included in your free usage") — see the
    #     denylist in tests/test_subagent_tiers.py.
    # A free-only registry cannot carry it.  Re-checked before every release.
    {"provider_id": "nvidia", "name": "minimaxai/minimax-m3.1-flash",               "max_tokens": 16384, "enable_thinking": True},
    {"provider_id": "nvidia", "name": "nvidia/nemotron-3-ultra-550b-a55b",     "max_tokens": 16384, "enable_thinking": True},
    {"provider_id": "nvidia", "name": "nvidia/nemotron-3.5-super-vl-120b-a12b",     "max_tokens": 16384, "enable_thinking": True},
    # Mistral Large 4 — 1T total / 52B active MoE.  Verified live 2026-11-06:
    # the route answers and emits a real tool call on NVIDIA.  It reasons
    # unconditionally (reasoning_content arrives before content), so the
    # enable_thinking flag below is accepted but has no effect on it.
    {"provider_id": "nvidia", "name": "mistralai/mistral-large-4-1t-a52b",        "max_tokens": 16384, "enable_thinking": True},
    {"provider_id": "nvidia", "name": "z-ai/glm-5.3-flash",           "max_tokens": 16384, "enable_thinking": True},
    {"provider_id": "nvidia", "name": "nvidia/nemotron-3.5-lightning-30b-a3b", "max_tokens": 16384, "enable_thinking": True},
    # Google — Gemini free tier.
    {"provider_id": "google", "name": "gemini-3.8-flash",               "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.7-flash",               "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.6-flash",               "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.5-flash",               "max_tokens": None},
    {"provider_id": "google", "name": "gemini-3.1-flash-lite",          "max_tokens": None},
    {"provider_id": "google", "name": "gemma-4-31b-it",                 "max_tokens": None},
    # Ollama — free plan.
    {"provider_id": "ollama", "name": "nemotron-3-ultra:cloud",         "max_tokens": None, "reasoning_effort": "high"},
    {"provider_id": "ollama", "name": "gpt-oss:120b-cloud",             "max_tokens": None, "reasoning_effort": "high"},
    {"provider_id": "ollama", "name": "nemotron-3-super",               "max_tokens": None, "reasoning_effort": "high"},
    {"provider_id": "ollama", "name": "gemma4:31b",                     "max_tokens": None, "reasoning_effort": "high"},
    # Groq — free plan.
    {"provider_id": "groq",       "name": "qwen/qwen3.8-27b",             "max_tokens": 4096},
    {"provider_id": "groq",       "name": "openai/gpt-oss-120b",          "max_tokens": 4096},
    # OpenRouter — zero-priced routes only.
    {"provider_id": "openrouter", "name": "nvidia/nemotron-3-ultra-550b-a55b:free", "max_tokens": None},
    {"provider_id": "openrouter", "name": "nvidia/nemotron-3.5-super-vl:free",       "max_tokens": None},
    {"provider_id": "openrouter", "name": "cohere/north-mini-code:free",            "max_tokens": None},
    {"provider_id": "openrouter", "name": "dots-studio/dots-3-note-preview:free",   "max_tokens": None},
    {"provider_id": "nvidia", "name": "nvidia/nemotron-3.5-lightning-30b-a3b", "max_tokens": 16384, "enable_thinking": True},
]


# ── FREE-ONLY DECISION MODEL SLOTS (wake relevance, make_decision tool) ─────────
# A *decision model* is not a chat model.  It answers one structured question
# instead of generating prose:
#
#   * "noul" (not-one-unknown-logic) — a yes/no question answered with a
#     calibrated probability in [0, 1];
#   * "choice" — a pick from a set of named options;
#   * "score" — a position on an ordered scale.
#
# OpenRouter serves decision models from a separate endpoint,
# POST /api/alpha/decisions, and *refuses* them on /chat/completions
# ("respan/span-01-lite:free is a decisions model and cannot be used with the
# chat/completions endpoint").  The two shapes therefore need two callers.
#
# Everything below was verified live on 2026-11-06:
#
#   * NVIDIA has no decisions endpoint at all.  /v1/decisions,
#     /v1/alpha/decisions and /v1/decision all answer HTTP 404, and the
#     free catalogue (84 ids) serves chat and text models only.  There is no
#     NVIDIA route in the *decision* part of the chain.
#   * OpenRouter's respan/span-01-lite:free is the only free decision model.
#     The API answers usage.cost = 0, so it is genuinely zero-priced, and a
#     noul question comes back in ~0.8 s with a probability.
#   * Respan accepts *noul* questions only: "Respan only accepts noul questions
#     whose instructions and criteria are present, plain strings".  Verified
#     live on 2026-11-06 against respan/span-01-lite:free, respan/span-01-lite
#     *and the paid respan/span-01* — a "choice" or a "score" question is
#     answered with HTTP 400 by every one of them.  The limit belongs to the
#     model family, not to the free tier.
#   * The Decisions API itself does implement the full typed contract
#     (noul + choice + score): typesafe/jev-1.13 answered a choice question
#     and a score question live on 2026-11-06, each with a full probability
#     distribution.  It is *paid* (usage.cost = 1.29e-05) and
#     typesafe/jev-1.13:free answers HTTP 404 "This model is unavailable for
#     free", so the free-only rule keeps it out of the registry — see
#     PAID_TYPED_DECISION_MODELS below.
#   * One request may carry *several* questions, and they count as a single
#     request: three noul questions in one call came back with three
#     probabilities, usage.cost = 0, in ~1 s.  That is what makes an emulated
#     score (one cumulative noul question per level) affordable.
#   * The noul contract, read off the live API: `instructions` is a plain
#     string, `criteria` is an object of plain strings keyed "true"/"false",
#     and `state` is the content to evaluate.  Respan accepts a string or an
#     object of the shape {input: [messages], output: message}; an arbitrary
#     object ({"customer_tier": ...}) is rejected with HTTP 400, so the chain
#     sends a string.
#   * The free allowance is 50 requests/day *per key*.  An exhausted key
#     answers 429 with limit_source=openrouter_free_tier_daily, a dead key
#     answers 401 "User not found", and the chain rotates keys around both.
#
# Every route is free-only and is covered by free_route_violations().
#
# Slots:
#   provider_id  — provider key from PROVIDERS
#   name         — model id as the provider serves it
#   api          — "decisions" (the Decisions API) or "chat" (chat/completions)
#   question     — the question types this route can answer
#   max_tokens   — output ceiling for the chat routes (the decisions route
#                  returns a number, not tokens)
#   enable_thinking — NVIDIA chat-template switch.  It is sent *only* when it
#                  is present: Gemini's OpenAI-compatible endpoint rejects the
#                  unknown field with HTTP 400 ("Unknown name
#                  chat_template_kwargs").
DECISION_API_URL = "https://openrouter.ai/api/alpha/decisions"

DECISION_MODEL_SLOTS: list[dict] = [
    {
        "provider_id": "openrouter",
        "name": "respan/span-01-lite:free",
        "api": "decisions",
        "question": ("noul",),
        "max_tokens": 0,
        "timeout": 12.0,
        "note": "the only free decision model — noul questions only, zero-priced",
    },
    {
        "provider_id": "nvidia",
        "name": "nvidia/nemotron-3.5-bolt-e4b",
        "api": "chat",
        "question": ("choice", "text"),
        "max_tokens": 128,
        "timeout": 12.0,
        "enable_thinking": False,
        "note": "a text model used as the choice classifier — ~0.5 s, free",
    },
    {
        "provider_id": "google",
        "name": "gemini-2.5-flash",
        "api": "chat",
        "question": ("noul", "choice", "text"),
        "max_tokens": 256,
        "timeout": 15.0,
        "note": "the free-tier fallback for every question type",
    },
]


#: Decision models that answer OpenRouter's *full* typed contract — noul,
#: choice and score, each with a probability distribution.
#:
#: None of them is registered, because every one of them is paid:
#: ``typesafe/jev-1.13`` answered a choice question and a score question live
#: on 2026-11-06 (usage.cost = 1.29e-05), while ``typesafe/jev-1.13:free``
#: answers HTTP 404 "This model is unavailable for free.  The paid version is
#: available now - use this slug instead: typesafe/jev-1.13".
#:
#: The list is recorded here so the fact does not have to be rediscovered, and
#: so nobody adds one of them to DECISION_MODEL_SLOTS by accident:
#: free_route_violations() rejects a paid route, and the project is free-only.
#: The typed question path in core/tools.py is written against this contract
#: and works the moment a free typed model appears.
PAID_TYPED_DECISION_MODELS: tuple[str, ...] = ("typesafe/jev-1.13",)


# ── SUB-AGENT INTELLIGENCE TIERS ────────────────────────────────────────────
# `delegate_subtask` does not take a model id any more.  It takes an
# intelligence level — "high", "medium" or "low" — and routes the work to the
# models registered for that level.  This registry is the single source of
# truth for that routing; the tool itself never hard-codes a model list.
#
# Three rules govern the registry:
#
#   1. FREE ONLY.  Termux-AI is built for free AI, so every route here works
#      without a credit card.  Each provider was asked what it considers free
#      (2026-10-30), and the answer was taken from the provider itself:
#
#        * Ollama Cloud — the account has a "Free plan"; a model outside it
#          answers HTTP 402 "this model is not included in your free usage".
#          That refusal is how the paid Ollama models were identified.
#        * Google — the Gemini Developer API free tier.  The service names its
#          own quota metric: "generate_content_free_tier_requests", quota id
#          "...-FreeTier", 5 requests per minute per model per project.  The
#          Pro models report a free-tier quota of 0, i.e. they are paid-only.
#        * Groq — the free plan.  Groq's docs state that the
#          x-ratelimit-limit-requests header always means requests per day;
#          the account reports 1000 RPD / 8000 TPM, exactly the published
#          Free Plan limits for gpt-oss-120b, gpt-oss-20b and qwen3.8-27b.
#        * NVIDIA — the free serverless API (build.nvidia.com).
#        * OpenRouter — the catalogue price.  A route is free only when the
#          published prompt *and* completion price are 0 (the ":free" slugs);
#          every paid slug answered 402 with a $0.00 balance.
#
#   2. Only tool-calling capable models are allowed.  A sub-agent that cannot
#      call a tool can only talk, which makes it useless for the work it is
#      delegated.  Every entry below was verified live (2026-10-30) by sending
#      a real chat completion with a `tools` payload and checking that the
#      model answered with a tool call — see TOOL_CALLING_MODELS and
#      tests/fixtures/subagent_tool_calling.json.
#
#   3. The entries of a tier are ordered by measured capability.  The primary
#      key is the Artificial Analysis intelligence index published in
#      OpenRouter's live catalogue; models the catalogue does not index are
#      placed by their flagship status and scale.  Routes that need minutes
#      for their first token are moved to the end of their tier, because a
#      delegation must not stall on a cold route while a warm one is free.
#      The headline evidence:
#
#        * Gemini 3.8 Flash   — AA intelligence 30.5 / coding 76.3, "our most
#                               intelligent Flash model" (Google's own words),
#                               1M context — the strongest model the free tier
#                               of the Gemini API serves.
#        * MiniMax M3.1 Flash — AA intelligence 28.9, mandatory reasoning,
#                               multimodal, 1M context.
#        * Qwen3.8-27B        — AA coding 68.1 / agentic 45.8.
#        * Nemotron 3.5 Super VL — AA intelligence 13.6, 120B multimodal.
#        * Nemotron 3 Ultra   — 550B total / 55B active MoE, "frontier
#                              reasoning", tuned for agent orchestration and
#                              tool calling (NVIDIA model card).
#        * Mistral Large 4    — 1T total / 52B active MoE.
#        * Step 5             — reasoning-focused preview model.
#        * GLM-5.3 Flash      — AA coding 71.5 / agentic 50.9 — but it needs
#                               ~180 s for its first token on the free
#                               endpoint, so it is tried last in its tier.
#        * GPT-OSS 120B       — OpenAI's flagship open-weight model; strong
#                              reasoning but weak agentic/tool-use numbers
#                              (AA coding 30.4 / agentic 3.7) → a tier down.
#        * Gemma 4 31B        — AA coding 43.4 / agentic 4.2 → a tier down.
#        * Gemini 2.5 Flash   — "best price-performance model for low-latency,
#                              high-volume tasks that require reasoning" →
#                              the cheap end of the pool.
#
# Each entry:
#   provider_id  — provider key from PROVIDERS
#   name         — model id as the provider serves it
#   max_tokens   — output ceiling.  It is a *ceiling*, not a target.  The
#                  sub-agent never guesses how much room a report needs, so
#                  this is None for every provider that answers until it is
#                  done when the field is missing (Google, NVIDIA, Ollama —
#                  verified with a long-answer probe).  Groq and OpenRouter
#                  are the exceptions: Groq silently truncates at 3072 tokens
#                  without the field, and OpenRouter checks the requested
#                  output budget against the account balance before it runs
#                  anything.  The runtime lowers that ceiling when the
#                  account cannot afford it and continues a truncated answer
#                  on the next route instead of dropping it.
#   reasoning    — request provider-native reasoning for this route
#   free         — the route works without paid credits (always True here)
#   note         — why the model sits in this tier
#
# The same model often appears on several providers, because each provider
# meters its own free allowance: when one is exhausted the others still work.

# ── FREE-ONLY ENFORCEMENT ─────────────────────────────────────────────────────
# Termux-AI is built for free AI.  That is a hard rule, not a preference, so
# it is enforced here instead of being left to whoever edits the lists above.
#
# The enforcement is *rule-based*, one rule per provider, because the
# providers disagree about what "free" means:
#
#   * OpenRouter — the catalogue price is the authority.  A slug is free only
#     when it ends in ":free", the catalogue's own marker for a zero prompt
#     *and* completion price.  Re-verified live on 2026-10-31 against the
#     live catalogue: every OpenRouter route this repo can select has
#     prompt=0 and completion=0.  Every other slug is paid.
#   * Ollama Cloud — the account has a Free plan.  A model outside that plan
#     refuses with HTTP 402 ("this model is not included in your free usage"),
#     so the free set is exactly the models that answered without the
#     refusal (re-verified live on 2026-10-31).  Every other cloud model is
#     paid.
#   * Google — the Gemini Developer API free tier covers the Flash, Flash-Lite
#     and Gemma chat families.  The Pro families publish a free-tier quota of
#     0, and every image model is billed (the "Flash" in the name does not put
#     an image model on the free tier — verified live 2026-10-30).
#   * NVIDIA — the free serverless API (build.nvidia.com).
#   * Groq — the free plan.
#   * ollama-local — the on-device server: free by construction.
#
# No paid model id is stored anywhere in this repository.  A blocklist of paid
# slugs can be dodged by an unlisted one, and a free-AI project has no
# business carrying the names of the models it refuses to call — the rules
# above reject every paid route, including the ones nobody probed yet.
#
# `free_route_violations()` is the single check used by the test suite, so a
# paid route cannot be reintroduced by accident.

# The Ollama Cloud models that are included in the account's Free plan.
# Verified live: each of them answered a completion without the 402 refusal.
# A model that is not in this set is paid and must never be routed.
# nemotron-3-nano:30b was re-verified on 2026-11-03 (it had been added to the
# low tier before the free set learned about it, so the free check rejected it).
OLLAMA_FREE_PLAN_MODELS: frozenset[str] = frozenset({
    "nemotron-3-ultra:cloud", "nemotron-3-super",
    "gpt-oss:120b-cloud", "gpt-oss:120b", "gpt-oss:20b",
    "gemma4:31b", "gemma4:31b-cloud", "beam:cloud",
    "nemotron-3-nano:30b",
})

# Providers whose every route is free by construction: the free serverless
# API (NVIDIA), the Free Plan (Groq) and the local Ollama server.
FREE_BY_CONSTRUCTION_PROVIDERS: frozenset[str] = frozenset({
    "nvidia", "groq", "ollama-local",
    # The hosted Visual Design image endpoints are free — verified live: a
    # FLUX.2 Klein request came back with the image inline.
    "nvidia-images",
})

# The Google model families the free tier does not serve: the Pro families
# publish a free-tier quota of 0 and every image model is billed.  Both are
# rejected by rule instead of by name.
_GOOGLE_BILLED_TAGS: tuple[str, ...] = ("pro", "argon", "image")


def is_free_route(provider_id: str, model_name: str | None) -> bool:
    """True when a route can be served without paid credits.

    The check is deliberately strict: a route is free only when the provider
    itself says so.  Unknown routes are treated as *not* free, because the
    cost of a wrong "free" verdict is a 402 in the middle of a request.
    """
    if not model_name:
        return False

    if provider_id == "openrouter":
        # OpenRouter publishes the price of every slug; ":free" is the
        # catalogue's own marker for a zero prompt *and* completion price.
        return model_name.endswith(":free")

    if provider_id == "google":
        # The Gemini Developer API free tier covers the Flash, Flash-Lite and
        # Gemma families.  The Pro families and every image model are billed.
        if any(tag in model_name for tag in _GOOGLE_BILLED_TAGS):
            return False
        return any(tag in model_name for tag in ("flash", "lite", "gemma"))

    if provider_id == "ollama":
        # Ollama Cloud meters a Free plan; the models on it were verified by
        # the absence of the 402 refusal.  Everything else is paid.
        return model_name in OLLAMA_FREE_PLAN_MODELS

    return provider_id in FREE_BY_CONSTRUCTION_PROVIDERS


def free_route_violations(registries: dict | None = None) -> list[tuple[str, str]]:
    """Every (provider_id, model) in the given registries that is not free.

    With no argument the four routable registries are checked: the chat
    rotation, the agent rotation, the decision chain and the sub-agent tiers.
    """
    if registries is None:
        registries = {
            "MODEL_SLOTS": MODEL_SLOTS,
            "AGENT_MODEL_SLOTS": AGENT_MODEL_SLOTS,
            "DECISION_MODEL_SLOTS": DECISION_MODEL_SLOTS,
            "SUBAGENT_TIERS": {
                level: [entry for entry in entries]
                for level, entries in SUBAGENT_TIERS.items()
            },
        }

    offenders: list[tuple[str, str]] = []
    for label, registry in registries.items():
        if label == "SUBAGENT_TIERS":
            for entries in registry.values():
                for entry in entries:
                    if not is_free_route(entry["provider_id"], entry["name"]):
                        offenders.append((entry["provider_id"], entry["name"]))
        else:
            for slot in registry:
                if not is_free_route(slot["provider_id"], slot["name"]):
                    offenders.append((slot["provider_id"], slot["name"]))
    return sorted(set(offenders))


SUBAGENT_INTELLIGENCE_LEVELS: tuple[str, ...] = ("high", "medium", "low")
DEFAULT_SUBAGENT_INTELLIGENCE = "medium"

# Which levels are tried for a request of a given level.  The ladder only
# ever goes *down*: a "medium" delegation must not silently run a frontier
# model, because the level is what the caller asked for.  A "high" task that
# nobody can serve still gets an answer from a cheaper model.
SUBAGENT_FALLBACK_LADDER: dict[str, tuple[str, ...]] = {
    "high": ("high", "medium", "low"),
    "medium": ("medium", "low"),
    "low": ("low",),
}

# Models verified to accept a `tools` payload and answer with a tool call.
# This is the hard gate for the sub-agent registry: a model that is not in
# this set must never be routed a delegated task.  Verified 2026-10-30 with
# a live request per model (see workspace/subagent_tiers/research/ and
# tests/fixtures/subagent_tool_calling.json).  The four routes that had
# drifted away from the evidence were re-probed live on 2026-11-03
# (workspace/subagent_tiers/research/reprobe_gap.py).
# Poolside Laguna models removed 2026-11-05: no longer available on OpenRouter
# or NVIDIA's free endpoint (HTTP 404 "no provider serves this model anymore").
TOOL_CALLING_MODELS: dict[str, set[str]] = {
    "openrouter": {
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3.5-super-vl:free",
        "nvidia/nemotron-3.5-lightning:free",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "cohere/north-mini-code:free",
        "dots-studio/dots-3-note-preview:free",
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
        "liquid/lfm-2.5-2.6b:free",
        "recursive/flint-v1.0:free",
        "xingchen-agi/xing4.0-29b-a4b:free",
    },
    "nvidia": {
        "nvidia/nemotron-3-ultra-550b-a55b",
        "nvidia/nemotron-3.5-super-vl-120b-a12b",
        "nvidia/nemotron-3.5-lightning-30b-a3b",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        "nvidia/nemotron-3.5-bolt-e4b",
        "minimaxai/minimax-m3.1-flash",
        "z-ai/glm-5.3-flash",
        "stepfun-ai/step-5-preview",
        "mistralai/mistral-large-4-1t-a52b",
        "google/gemma-4-31b-it",
        "openai/gpt-oss-20b",
    },
    "groq": {
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
    },
    "google": {
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
        "gemini-flash-latest",
        "gemini-flash-lite-latest",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemma-4-31b-it",
        "gemma-4-26b-a4b-it",
    },
    "ollama": {
        "beam:cloud",
        "nemotron-3-ultra:cloud",
        "nemotron-3-super",
        "gpt-oss:120b-cloud",
        "gpt-oss:120b",
        "gpt-oss:20b",
        "gemma4:31b-cloud",
        # gemma4:31b and nemotron-3-nano:30b were already routed by the tiers
        # and already carried live evidence, but the gate never learned about
        # them — so the tiers held two models the gate refused to recognise.
        # Both were re-probed live on 2026-11-03 (free plan, tool call).
        "gemma4:31b",
        "nemotron-3-nano:30b",
    },
}

# HIGH — the largest and the reasoning-capable models.  Everything that can
# chew through a hard, multi-step engineering task lives here.
SUBAGENT_TIERS: dict[str, list[dict]] = {
    "high": [
        {
            "provider_id": "google",
            "name": "gemini-3.8-flash",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "AA intelligence 30.5 / coding 76.3 — the strongest model the Gemini free tier serves",
        },
        {
            "provider_id": "nvidia",
            "name": "minimaxai/minimax-m3.1-flash",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "AA intelligence 28.9, mandatory reasoning, 1M context — free NVIDIA endpoint",
        },
        {
            "provider_id": "groq",
            "name": "qwen/qwen3.8-27b",
            "max_tokens": 3072,
            "reasoning": True,
            "free": True,
            "note": "AA coding 68.1 / agentic 45.8 — Groq free plan (8K tokens/minute)",
        },
        {
            "provider_id": "nvidia",
            "name": "nvidia/nemotron-3.5-super-vl-120b-a12b",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "AA intelligence 13.6 — 120B multimodal reasoning MoE",
        },
        {
            "provider_id": "openrouter",
            "name": "nvidia/nemotron-3.5-super-vl:free",
            "max_tokens": 32768,
            "reasoning": True,
            "free": True,
            "note": "the same weights on the zero-priced OpenRouter route",
        },
        {
            "provider_id": "ollama",
            "name": "beam:cloud",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "Beam is an open model by Reflection designed to deliver strong performance for areas such as agentic tool use, coding and general reasoning",
        },
        {
            "provider_id": "nvidia",
            "name": "nvidia/nemotron-3-ultra-550b-a55b",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "Nemotron 3 Ultra — 550B total / 55B active MoE, frontier reasoning",
        },
        {
            "provider_id": "ollama",
            "name": "nemotron-3-ultra:cloud",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the same Nemotron 3 Ultra flagship on the Ollama free plan",
        },
        {
            "provider_id": "openrouter",
            "name": "nvidia/nemotron-3-ultra-550b-a55b:free",
            "max_tokens": 32768,
            "reasoning": True,
            "free": True,
            "note": "the same flagship, 1M context, zero-priced route",
        },
        {
            "provider_id": "nvidia",
            "name": "stepfun-ai/step-5-preview",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "Step 5 — reasoning-focused preview model on the free NVIDIA endpoint",
        },
        {
            "provider_id": "nvidia",
            "name": "mistralai/mistral-large-4-1t-a52b",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "Mistral Large 4 — 1T total / 52B active MoE, reasoning-first; "
                   "verified live 2026-11-06 to answer and to emit a real tool call on NVIDIA",
        },
        {
            "provider_id": "google",
            "name": "gemini-3.7-flash",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "AA coding 76.1 / agentic 35.2",
        },
        {
            "provider_id": "nvidia",
            "name": "z-ai/glm-5.3-flash",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "AA coding 71.5 / agentic 50.9 — but ~180 s for the first token, so it is tried last",
        },
    ],
    "medium": [
        {
            "provider_id": "ollama",
            "name": "gpt-oss:120b-cloud",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "GPT-OSS 120B on the Ollama free plan — OpenAI's flagship open-weight model",
        },
        {
            "provider_id": "groq",
            "name": "openai/gpt-oss-120b",
            "max_tokens": 3072,
            "reasoning": True,
            "free": True,
            "note": "the same GPT-OSS 120B weights on the Groq free plan",
        },
        {
            "provider_id": "ollama",
            "name": "gpt-oss:120b",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "GPT-OSS 120B on the Ollama free plan (non-cloud alias)",
        },
        {
            "provider_id": "ollama",
            "name": "nemotron-3-super",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "Nemotron 3 Super — 120B reasoning MoE, AA coding 37.7",
        },
        {
            "provider_id": "google",
            "name": "gemini-3.6-flash",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "AA coding 69.2 / agentic 29.0",
        },
        {
            "provider_id": "google",
            "name": "gemini-3.5-flash",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "AA coding 70.1 / agentic 26.0",
        },
        {
            "provider_id": "ollama",
            "name": "gemma4:31b",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "Gemma 4 31B on the Ollama free plan (AA coding 43.4)",
        },
        {
            "provider_id": "google",
            "name": "gemma-4-31b-it",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the same Gemma 4 31B weights on the Gemini free tier",
        },
        {
            "provider_id": "openrouter",
            "name": "google/gemma-4-31b-it:free",
            "max_tokens": 32768,
            "reasoning": True,
            "free": True,
            "note": "the same weights on the zero-priced OpenRouter route",
        },
        {
            "provider_id": "openrouter",
            "name": "dots-studio/dots-3-note-preview:free",
            "max_tokens": 32768,
            "reasoning": True,
            "free": True,
            "note": "free note-taking model with a 512K context",
        },
        {
            "provider_id": "nvidia",
            "name": "google/gemma-4-31b-it",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the same weights on NVIDIA — but ~170 s for the first token, so it is tried last",
        },
    ],
    "low": [
        {
            "provider_id": "google",
            "name": "gemini-2.5-flash",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the cheap end of the pool — low latency, still reasons",
        },
        {
            "provider_id": "google",
            "name": "gemini-2.5-flash-lite",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the smaller sibling of Gemini 2.5 Flash",
        },
        {
            "provider_id": "google",
            "name": "gemini-3.1-flash-lite",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the cheapest of the 3.x Flash family",
        },
        {
            "provider_id": "google",
            "name": "gemini-3.5-flash-lite",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the lite variant of the 3.5 Flash family",
        },
        {
            "provider_id": "google",
            "name": "gemini-flash-lite-latest",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the rolling alias of the lite Flash model",
        },
        {
            "provider_id": "google",
            "name": "gemini-flash-latest",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the rolling alias of the Flash model",
        },
        {
            "provider_id": "google",
            "name": "gemini-3-flash-preview",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the preview build of the 3 Flash family",
        },
        {
            "provider_id": "google",
            "name": "gemma-4-26b-a4b-it",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the smaller Gemma 4 (AA coding 39.3)",
        },
        {
            "provider_id": "openrouter",
            "name": "google/gemma-4-26b-a4b-it:free",
            "max_tokens": 32768,
            "reasoning": True,
            "free": True,
            "note": "the same weights on the zero-priced OpenRouter route",
        },
        {
            "provider_id": "openrouter",
            "name": "nvidia/nemotron-3.5-lightning:free",
            "max_tokens": 32768,
            "reasoning": True,
            "free": True,
            "note": "the same weights on the zero-priced OpenRouter route",
        },
        {
            "provider_id": "nvidia",
            "name": "nvidia/nemotron-3.5-lightning-30b-a3b",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the same 30B Lightning weights on the free NVIDIA endpoint",
        },
        {
            "provider_id": "nvidia",
            "name": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "a 30B omni model that reasons (AA coding 13.8)",
        },
        {
            "provider_id": "openrouter",
            "name": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            "max_tokens": 32768,
            "reasoning": True,
            "free": True,
            "note": "the same weights on the zero-priced OpenRouter route",
        },
        {
            "provider_id": "ollama",
            "name": "nemotron-3-nano:30b",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the 30B Nemotron on the Ollama free plan",
        },
        {
            "provider_id": "ollama",
            "name": "gpt-oss:20b",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the small sibling of GPT-OSS (AA coding 20.7)",
        },
        {
            "provider_id": "groq",
            "name": "openai/gpt-oss-20b",
            "max_tokens": 3072,
            "reasoning": True,
            "free": True,
            "note": "the same weights on the Groq free plan",
        },
        {
            "provider_id": "nvidia",
            "name": "openai/gpt-oss-20b",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "the same weights on the free NVIDIA endpoint",
        },
        {
            "provider_id": "openrouter",
            "name": "cohere/north-mini-code:free",
            "max_tokens": 32768,
            "reasoning": True,
            "free": True,
            "note": "a small code model (AA coding 36.5) on the zero-priced route",
        },
        {
            "provider_id": "openrouter",
            "name": "liquid/lfm-2.5-2.6b:free",
            "max_tokens": 32768,
            "reasoning": True,
            "free": True,
            "note": "a 2.6B model — the smallest useful fallback",
        },
        {
            "provider_id": "openrouter",
            "name": "recursive/flint-v1.0:free",
            "max_tokens": 32768,
            "reasoning": True,
            "free": True,
            "note": "a small zero-priced model with a 256K context",
        },
        {
            "provider_id": "openrouter",
            "name": "xingchen-agi/xing4.0-29b-a4b:free",
            "max_tokens": 32768,
            "reasoning": True,
            "free": True,
            "note": "a 29B mixture-of-experts model on the zero-priced route",
        },
        {
            "provider_id": "ollama",
            "name": "beam:cloud",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "a fast model of the Ollama free plan — kept as a last resort",
        },
        {
            "provider_id": "nvidia",
            "name": "nvidia/nemotron-3.5-bolt-e4b",
            "max_tokens": None,
            "reasoning": True,
            "free": True,
            "note": "a 4B model — the smallest route that still calls tools",
        },
    ],
}


def normalize_intelligence_level(level: str | None) -> str:
    """Map a caller-supplied level onto one of the three known levels."""
    if not isinstance(level, str):
        return DEFAULT_SUBAGENT_INTELLIGENCE
    value = level.strip().lower()
    if value in SUBAGENT_INTELLIGENCE_LEVELS:
        return value
    if value in ("hi", "strongest", "best", "max"):
        return "high"
    if value in ("mid", "default", "normal", "balanced"):
        return "medium"
    if value in ("lo", "cheap", "fast", "minimal"):
        return "low"
    return DEFAULT_SUBAGENT_INTELLIGENCE


def subagent_tier(level: str | None) -> list[dict]:
    """The registered models of one intelligence level."""
    return SUBAGENT_TIERS.get(normalize_intelligence_level(level), [])


def get_subagent_rotation(level: str | None) -> list[dict]:
    """The routing order for one intelligence level.

    The registry order is kept, so the first route that answers is also the
    most capable model of the level.  The level itself is tried before the
    level below it — a hard task that every model of its level refuses is
    better served by a slightly weaker model that answers than by an empty
    report.
    """
    tier_name = normalize_intelligence_level(level)
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(e: dict, t: str) -> None:
        key = (e["provider_id"], e["name"])
        if key in seen:
            return
        seen.add(key)
        entry = dict(e)
        entry["tier"] = t
        entries.append(entry)

    for t in SUBAGENT_FALLBACK_LADDER.get(tier_name, (tier_name,)):
        for e in SUBAGENT_TIERS.get(t, []):
            _add(e, t)
    return entries


def subagent_model_ids(level: str | None = None) -> list[str]:
    """Human-readable list of the models a level routes to."""
    levels = [level] if level else list(SUBAGENT_INTELLIGENCE_LEVELS)
    out: list[str] = []
    for lvl in levels:
        for e in SUBAGENT_TIERS.get(normalize_intelligence_level(lvl), []):
            out.append(f"{e['provider_id']}/{e['name']}")
    return out


# ── VISION (MULTIMODAL IMAGE INPUT) ────────────────────────────────────────────
# Models whose chat endpoint accepts an image next to the text, using the
# OpenAI-style content-part shape:
#
#   {"role": "user", "content": [
#       {"type": "text", "text": "..."},
#       {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
#   ]}
#
# Every entry below was verified live by sending a real PNG and checking that
# the request was accepted.  Models that were *rejected* are deliberately
# absent, and the absence is itself evidence:
#
#   * nvidia/nemotron-3-ultra-550b-a55b and nvidia/nemotron-3.5-lightning-30b-a3b
#     answer "Received multimodal data but multimodal processing is not enabled".
#   * ollama gpt-oss:120b-cloud and nemotron-3-ultra:cloud answer
#     "this model does not support image input".
#   * groq answers "messages[0].content must be a string" — it has no
#     multimodal content shape at all.
#
# The entries marked "alias" are floating aliases of a verified multimodal
# model that were rate-limited or timed out rather than rejected during the
# probe.  They are safe to use because a model that turns out to be
# text-only triggers the image-stripping retry in core/llm_client.py.
VISION_CAPABLE_MODELS: dict[str, set[str]] = {
    "nvidia": {
        # verified: image round-trip accepted and answered
        "nvidia/nemotron-3.5-super-vl-120b-a12b",
        "minimaxai/minimax-m3.1-flash",
    },
    "google": {
        # verified
        "gemini-3.5-flash",
        "gemini-3.6-flash",
        "gemini-3-flash-preview",
        "gemini-3.1-flash-lite",
        "gemini-3.1-flash-lite-preview",
        "gemini-flash-lite-latest",
        "gemma-4-31b-it",
        "gemma-4-26b-a4b-it",
        # alias: quota-limited / overloaded during the probe, never rejected
        "gemini-flash-latest",
        "gemini-3.7-flash",
        # The Pro families are deliberately absent: they publish a free-tier
        # quota of 0, so they are paid and a free-AI repo must not route them.
    },
    "ollama": {
        "gemma4:31b",
    },
}


def model_supports_vision(provider_id: str, model_name: str | None) -> bool:
    """Return True when the model's endpoint accepts image input.

    The check is exact-match against the verified registry.  A model that is
    not listed is treated as text-only, so an image is never attached to a
    request that would reject it.
    """
    if not model_name:
        return False
    return model_name in VISION_CAPABLE_MODELS.get(provider_id, set())


def model_supports_tools(provider_id: str, model_name: str | None) -> bool:
    """Return True when the model was verified to call tools.

    A delegated sub-task is only useful if the worker can act on it, so the
    sub-agent registry is restricted to the models in TOOL_CALLING_MODELS.
    The check is exact-match against the verified registry; an unknown model
    is treated as text-only.
    """
    if not model_name:
        return False
    return model_name in TOOL_CALLING_MODELS.get(provider_id, set())


# ── IMAGE GENERATION MODELS ───────────────────────────────────────────────────
# Image models get their own registry instead of joining MODEL_SLOTS /
# AGENT_MODEL_SLOTS: those two drive the chat-completions rotation, and every
# entry in them is a text/chat model.  Image generation uses different request
# and response shapes, so mixing the two would corrupt the chat rotation.
#
# api kinds:
#   "google-image" — OpenAI-compatible chat endpoint; the image arrives inline
#                    (data URI in the message content, or message.images[]).
#   "nvidia-genai" — POST {base_url}/genai/{name}; the image arrives as
#                    artifacts[].base64 (JPEG) inside the JSON body.
#
# Every NVIDIA envelope below was read off the live service, not from the
# published docs: NVIDIA's reference pages 404 for most of these models and
# the catalogue page disagrees with the running API on several points.
# The bounds were recovered with a "schema oracle" trick — the endpoints
# validate the request body *before* running inference, so a malformed body
# comes back as a 422 that names the offending field even while the happy
# path is broken.
#
# size_enum: the exact width/height literals the endpoint accepts.  These
#   differ per model, so they are recorded per slot rather than shared.
# hosted_available: False marks a catalogue model that NVIDIA lists but does
#   not serve from its hosted free API (it 404s; it is a self-hosted NIM).
IMAGE_MODEL_SLOTS: list[dict] = [
    # ── Google — Gemini native image generation ("Nano Banana") ─────────────
    # Every one of these is billed: the service answers with a free-tier
    # request limit of 0 (verified live 2026-10-30, see
    # workspace/subagent_tiers/research/google_image_free_tier.json).  The
    # "Flash" in the name does not put an image model on the free tier.
    # They stay in the catalogue because the tool can still be pointed at
    # them explicitly, but they are never the automatic choice — the
    # free path is NVIDIA's hosted FLUX endpoints below.
    {"provider_id": "google", "api": "google-image",
     "name": "gemini-3.1-flash-image-preview", "label": "Gemini 3.1 Flash Image",
     "free": False},
    {"provider_id": "google", "api": "google-image",
     "name": "gemini-3-pro-image-preview", "label": "Gemini 3 Pro Image",
     "free": False},
    {"provider_id": "google", "api": "google-image",
     "name": "gemini-3.1-flash-image", "label": "Gemini 3.1 Flash Image (GA)",
     "free": False},
    {"provider_id": "google", "api": "google-image",
     "name": "gemini-3-pro-image", "label": "Gemini 3 Pro Image (GA)",
     "free": False},
    {"provider_id": "google", "api": "google-image",
     "name": "gemini-3.1-flash-lite-image", "label": "Gemini 3.1 Flash Lite Image",
     "free": False},
    {"provider_id": "google", "api": "google-image",
     "name": "gemini-2.5-flash-image", "label": "Gemini 2.5 Flash Image",
     "free": False},

    # ── NVIDIA — the full image catalogue on ai.api.nvidia.com ──────────────
    {
        "provider_id": "nvidia-images",
        "api": "nvidia-genai",
        "name": "black-forest-labs/flux-3-image-klein",
        "label": "FLUX.3 Image Klein",
        # cfg_scale 1..20, steps 1..50, mode 't2i'|'i2i',
        # negative_prompt accepted.
        "cfg_scale": {"min": 1.0, "max": 20.0, "default": 3.5},
        "steps": {"min": 1, "max": 50, "default": 30},
        "prompt_max_len": 2000,
        "size_max_pixels": 1062400,
        "size_enum": tuple(range(512, 1569, 16)),
        "supports_negative_prompt": True,
        "supports_image_input": True,
        "hosted_available": True,
    },
    {
        "provider_id": "nvidia-images",
        "api": "nvidia-genai",
        "name": "black-forest-labs/flux.2-klein-4b",
        "label": "FLUX.2 Klein 4B",
        # cfg_scale is capped at exactly 1 by the live endpoint and steps at 4.
        # `mode` and `negative_prompt` are *rejected* as extra fields here.
        "cfg_scale": {"min": 0.0, "max": 1.0, "default": 1.0},
        "steps": {"min": 1, "max": 4, "default": 4},
        "prompt_max_len": 10000,
        "size_max_pixels": 1062400,
        "size_enum": tuple(range(512, 1569, 16)),
        "supports_negative_prompt": False,
        "supports_image_input": True,
        "hosted_available": True,
    },
    {
        "provider_id": "nvidia-images",
        "api": "nvidia-genai",
        "name": "black-forest-labs/flux.1-dev",
        "label": "FLUX.1 Dev",
        # cfg_scale must be strictly greater than 1; steps 5..100;
        # mode 'base'|'canny'|'depth'.
        "cfg_scale": {"min": 1.0, "max": 9.0, "default": 5.0, "exclusive_min": True},
        "steps": {"min": 5, "max": 100, "default": 50},
        "prompt_max_len": 10000,
        "size_max_pixels": 1062400,
        "size_enum": (768, 832, 896, 960, 1024, 1088, 1152, 1216, 1280, 1344),
        "supports_negative_prompt": False,
        "supports_image_input": False,
        "hosted_available": True,
    },
    {
        "provider_id": "nvidia-images",
        "api": "nvidia-genai",
        "name": "black-forest-labs/flux.1-schnell",
        "label": "FLUX.1 Schnell",
        # cfg_scale >= 0, steps <= 4, mode 'base' only.
        "cfg_scale": {"min": 0.0, "max": 1.0, "default": 0.0},
        "steps": {"min": 1, "max": 4, "default": 4},
        "prompt_max_len": 10000,
        "size_max_pixels": 1062400,
        "size_enum": (768, 832, 896, 960, 1024, 1088, 1152, 1216, 1280, 1344),
        "supports_negative_prompt": False,
        "supports_image_input": False,
        "hosted_available": True,
    },
    {
        "provider_id": "nvidia-images",
        "api": "nvidia-genai",
        "name": "black-forest-labs/flux.1-kontext-dev",
        "label": "FLUX.1 Kontext Dev",
        # Image-EDITING model: `image` is required, so it can only be used
        # with an input image.  cfg_scale > 1 and <= 9, steps 5..100.
        # `mode` and `negative_prompt` are rejected as extra fields.
        "cfg_scale": {"min": 1.0, "max": 9.0, "default": 3.5, "exclusive_min": True},
        "steps": {"min": 5, "max": 100, "default": 30},
        "prompt_max_len": 10000,
        "size_max_pixels": 1062400,
        "size_enum": (672, 688, 720, 752, 800, 832, 880, 944, 1024, 1104,
                      1184, 1248, 1328, 1392, 1456, 1504, 1568),
        "supports_negative_prompt": False,
        "supports_image_input": True,
        "requires_image": True,
        "hosted_available": True,
    },
    # ── NVIDIA catalogue models that are NOT on the hosted free API ─────────
    # These appear in build.nvidia.com's image catalogue but return 404 from
    # ai.api.nvidia.com/v1/genai — they are self-hosted NIMs (they need a
    # Blackwell/Hopper GPU and the SGlang Diffusion runtime).  They are listed
    # so the model catalogue is complete, but the rotation skips them and a
    # direct request explains the situation instead of burning a request.
    {
        "provider_id": "nvidia-images",
        "api": "nvidia-genai",
        "name": "qwen/qwen-image",
        "label": "Qwen Image (self-hosted)",
        "cfg_scale": {"min": 1.0, "max": 20.0, "default": 4.0},
        "steps": {"min": 1, "max": 50, "default": 20},
        "prompt_max_len": 10000,
        "size_max_pixels": 1062400,
        "size_enum": tuple(range(512, 1569, 16)),
        "supports_negative_prompt": False,
        "supports_image_input": True,
        "hosted_available": False,
        "hosted_note": ("qwen/qwen-image is a self-hosted NVIDIA NIM (SGlang "
                        "Diffusion on Blackwell/Hopper). It is not served from "
                        "ai.api.nvidia.com and returns HTTP 404 there."),
    },
    {
        "provider_id": "nvidia-images",
        "api": "nvidia-genai",
        "name": "qwen/qwen-image-edit",
        "label": "Qwen Image Edit (self-hosted)",
        "cfg_scale": {"min": 1.0, "max": 20.0, "default": 4.0},
        "steps": {"min": 1, "max": 50, "default": 20},
        "prompt_max_len": 10000,
        "size_max_pixels": 1062400,
        "size_enum": tuple(range(512, 1569, 16)),
        "supports_negative_prompt": False,
        "supports_image_input": True,
        "requires_image": True,
        "hosted_available": False,
        "hosted_note": ("qwen/qwen-image-edit is a self-hosted NVIDIA NIM and "
                        "is not served from ai.api.nvidia.com (HTTP 404)."),
    },
    {
        "provider_id": "nvidia-images",
        "api": "nvidia-genai",
        "name": "nvidia/qwen-image-edit-nvpcb-ovsl2sl",
        "label": "Qwen Image Edit PCB (self-hosted)",
        "cfg_scale": {"min": 1.0, "max": 20.0, "default": 4.0},
        "steps": {"min": 1, "max": 50, "default": 20},
        "prompt_max_len": 10000,
        "size_max_pixels": 1062400,
        "size_enum": tuple(range(512, 1569, 16)),
        "supports_negative_prompt": False,
        "supports_image_input": True,
        "requires_image": True,
        "hosted_available": False,
        "hosted_note": ("nvidia/qwen-image-edit-nvpcb-ovsl2sl is a self-hosted "
                        "NVIDIA NIM for PCB inspection and is not served from "
                        "ai.api.nvidia.com (HTTP 404)."),
    },
    {
        "provider_id": "nvidia-images",
        "api": "nvidia-genai",
        "name": "stabilityai/stable-diffusion-3.5-large",
        "label": "Stable Diffusion 3.5 Large (self-hosted)",
        "cfg_scale": {"min": 1.0, "max": 10.0, "default": 4.0},
        "steps": {"min": 1, "max": 100, "default": 30},
        "prompt_max_len": 10000,
        "size_max_pixels": 1062400,
        "size_enum": tuple(range(512, 1569, 16)),
        "supports_negative_prompt": True,
        "supports_image_input": False,
        "hosted_available": False,
        "hosted_note": ("stabilityai/stable-diffusion-3.5-large is a "
                        "self-hosted NVIDIA NIM and is not served from "
                        "ai.api.nvidia.com (HTTP 404)."),
    },
]
