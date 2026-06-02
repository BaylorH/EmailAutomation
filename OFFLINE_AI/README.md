# Offline AI Codebase Exploration (iPhone)

Goal: talk to an AI about this codebase with **no internet**, from an iPhone.

## Reality check
Claude itself can't run offline. A phone-sized open-weight LLM can — but it holds
only ~4K–32K tokens at once, while this repo is ~435K tokens. So you can't load the
whole repo. Instead, load a **condensed brief** for high-level questions, and paste
**individual files** for deep dives.

## One-time setup (do this while you still have wifi)
1. Install an offline LLM app: **Private LLM** (paid, polished), **PocketPal** or
   **LLM Farm** (free), or **fullmoon** (free).
2. Download a code model:
   - 8GB iPhone (15 Pro / 16): **Qwen2.5-Coder-7B-Instruct** (quantized).
   - 6GB / older: **Qwen2.5-Coder-3B** or **Llama 3.2 3B**.
3. Save these files to the iPhone **Files app** (AirDrop or the share sheet).

## How to use it offline
- **High-level questions** ("how does follow-up work?", "where are notifications
  created?"): load `codebase_core.md` (small) or `codebase_brief.md` (fuller) as the
  model's context / system prompt, then ask away.
- **Deep dive on one module**: open `codebase_full.md`, search the filename, copy that
  delimited block, paste it into the chat, then ask about it.
- Keep `CLAUDE.md` handy too — it's the architecture doc.

## Expectations
A phone model is far weaker than Claude and can hallucinate about code. It's good for
orientation and explaining code you paste in — not for reasoning over the whole system
at once. Treat its answers as a knowledgeable-but-fallible guide, and verify against
the actual source.

## Files
- `codebase_core.md`  — ~6.6K tokens. Core `email_automation/` map. Fits any model.
- `codebase_brief.md` — ~22K tokens. Full module map (all 80 source files).
- `codebase_full.md`  — full raw source, delimited per file. For chunked pasting only.
