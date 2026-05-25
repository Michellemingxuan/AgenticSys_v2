---
name: project-bureau-credit-scores
description: Domain context on bureau credit scores (FICO, LN, FSS/CSS/Paydex) — what each measures, consumer vs business, and why commercial customers blend both
metadata:
  type: project
---

## Consumer credit scores

- **FICO** is the standard bureau credit score — when users/analysts say "credit score" they mean FICO.
- The variant in use is **FICO 7 or FICO 8** (to be confirmed). This variant primarily evaluates **unsecured credit**, which aligns with the business's product focus (cards/unsecured lending).
- FICO does **not** fully capture other dimensions like mortgage exposure.

## LexisNexis (LN)

- **LN** = LexisNexis, an alternative credit-scoring provider.
- LN scores use **different model inputs** than FICO, so they provide a complementary dimension of the customer's credit profile.
- **FICO generally takes precedence** in decisioning, but LN adds breadth (e.g. public-records, identity-linked risk signals FICO may underweight).

## Business credit scores

- **FSS, CSS, Paydex, LN Business** are scores for the **business entity** (commercial portfolio), not the individual consumer.
- They measure the creditworthiness of the business the person owns.

## Consumer + Business linkage (commercial customers)

- Commercial customers are typically **small business owners**.
- The owner and the business are treated as "two sides of the same coin" — their risk is correlated.
- **Why:** if the business starts going bankrupt, the owner may over-leverage personal consumer credit (cards) to save the business, or vice versa. So both consumer and business scores matter for commercial-portfolio risk assessment.

**How to apply:** when specialists analyze credit-score columns, interpret FICO as the primary consumer score, LN as a supplementary consumer dimension, and FSS/CSS/Paydex/LN-Business as commercial-entity scores. Questions about "credit score" without qualification should default to FICO. For commercial customers, analysis should consider both consumer and business score trends together.
