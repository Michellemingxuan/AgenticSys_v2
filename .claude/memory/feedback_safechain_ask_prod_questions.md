---
name: feedback-safechain-ask-prod-questions
description: For safechain-related work, proactively raise concrete clarifying questions about the safechain setup/library/behavior — the user fetches ground-truth from the private env and reports back.
metadata:
  type: feedback
---

When working on a **safechain-related fix or investigation**, do NOT guess at safechain's API or runtime behavior. safechain is prod-only — it isn't installed in dev, so it can't be imported, introspected, or reproduced here ([[safechain_dual_environment]]). Instead, **surface the specific unknowns as concrete questions the user can answer from the private env**, then wait for the ground-truth before committing to an approach.

**Why:** The dev/prod gap is the recurring blocker on safechain bugs. This session, the correct fix (async-native `ainvoke`) only became possible after the user checked the private env and confirmed the safechain model is real-async (`_agenerate`/`_astream`) — see [[safechain_async_and_thread_occupation]]. Guessing would have produced a worse fix (e.g. naively restoring a reverted thread pool).

**How to apply:**
1. While debugging a safechain issue, list every fact about safechain I can't determine from this repo (API shape, threading/async model, timeout knobs, auth/token flow, error formats, what a class overrides, concurrency behavior).
2. Phrase each as a **concrete check the user can run in the private env** — a one-liner to execute, a source file/attribute to look up, or a quick experiment to time/observe — not a vague "how does safechain work?".
3. Present them up front (batch them), pause, and design the fix around the answers they bring back.
4. Still write dev-runnable tests by **mocking** the confirmed safechain behavior; flag that final validation happens in prod.
