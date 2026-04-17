# Agentic Case Review v7 — POC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a working POC of the v7 agentic case review system — Base Agent + domain skills, General Specialist Compare, specialist reuse, firewall retry, simulated data, developer JSON logs.

**Architecture:** Single-process monolith. One Base Specialist Agent configured via domain skills + pillar YAML. General Specialist detects cross-domain contradictions via Compare skill with self-Q&A. Session registry keeps specialists warm across questions. Firewall retry stack handles bidirectional SafeChain firewall rejections.

**Tech Stack:** Python 3.11+, OpenAI SDK (`openai`), Pydantic 2.x, PyYAML, Flask, NumPy (data generation), pytest.

**Spec:** `docs/specs/2026-04-17-agentic-case-review-v7-design.md`

---

## Task 1: Project Scaffolding & Dependencies

**Files:**
- Create: `requirements.txt`
- Create: `pyproject.toml`
- Create: all `__init__.py` files for package structure

- [ ] **Step 1: Create requirements.txt**

```
openai>=1.30.0,<2.0.0
pydantic>=2.0.0,<3.0.0
python-dotenv>=1.0.0,<2.0.0
pyyaml>=6.0.0,<7.0.0
flask>=3.0.0,<4.0.0
flask-cors>=4.0.0,<7.0.0
numpy>=1.26.0,<3.0.0
rich>=13.0.0,<15.0.0
pytest>=8.0.0,<9.0.0
```

- [ ] **Step 2: Create pyproject.toml**

```toml
[project]
name = "agentic-case-review-v1"
version = "0.1.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

- [ ] **Step 3: Create package structure**

Create these empty `__init__.py` files:

```
gateway/__init__.py
data/__init__.py
agents/__init__.py
skills/__init__.py
skills/domain/__init__.py
orchestrator/__init__.py
logging/__init__.py  (name this file: log/__init__.py to avoid shadowing stdlib logging)
tools/__init__.py
tests/__init__.py
tests/test_gateway/__init__.py
tests/test_data/__init__.py
tests/test_agents/__init__.py
tests/test_skills/__init__.py
tests/test_orchestrator/__init__.py
config/__init__.py
config/pillars/  (directory only, no __init__)
config/data_profiles/  (directory only, no __init__)
```

**Important:** Use `log/` instead of `logging/` for the logging package to avoid shadowing Python's stdlib `logging` module.

- [ ] **Step 4: Create .env.example**

```
OPENAI_API_KEY=sk-...
LOG_LEVEL=DEBUG
LOG_DIR=logs
```

- [ ] **Step 5: Create .gitignore**

Append to existing `.gitignore`:
```
__pycache__/
*.pyc
.env
logs/
data/simulated/
.superpowers/
venv/
.venv/
```

- [ ] **Step 6: Install dependencies and verify**

Run: `pip install -r requirements.txt`
Run: `python -c "import pydantic, yaml, openai, flask, numpy; print('OK')"`
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: scaffold v7 POC project structure and dependencies"
```

---

## Task 2: Pydantic Models — Shared Types

**Files:**
- Create: `models/__init__.py`
- Create: `models/types.py`
- Create: `tests/test_models/__init__.py`
- Create: `tests/test_models/test_types.py`

All downstream code depends on these types. Define them first.

- [ ] **Step 1: Write tests for core types**

```python
# tests/test_models/test_types.py
import pytest
from models.types import (
    DomainSkill, SpecialistOutput, SynthesisResult, ReportSection,
    AnswerResult, DataRequestResult, ReviewReport, Resolution,
    Conflict, FinalOutput, DataGap, BlockedStep, LLMResult, StepRecord,
)


def test_domain_skill_creation():
    skill = DomainSkill(
        name="bureau",
        system_prompt="You are a bureau data expert.",
        data_hints=["bureau_full", "bureau_trades"],
        interpretation_guide="Focus on tradeline health and derogatory marks.",
        risk_signals=["90D+ delinquency", "score below 600"],
    )
    assert skill.name == "bureau"
    assert len(skill.data_hints) == 2


def test_specialist_output_creation():
    output = SpecialistOutput(
        domain="bureau",
        question="What is the delinquency trajectory?",
        mode="chat",
        findings="3 derog marks in last 12 months, score declining.",
        evidence=["bureau_full.derog_count = 3", "score dropped 680 → 620"],
        implications=["Delinquency risk is elevated and worsening."],
        data_gaps=[],
        raw_data={"bureau_full": [{"score": 620, "derog_count": 3}]},
    )
    assert output.domain == "bureau"
    assert len(output.evidence) == 2


def test_review_report_with_resolution():
    resolution = Resolution(
        pair=("bureau", "spend_payments"),
        contradiction="Bureau says low risk but payments show deterioration",
        question_raised="Is the bureau score lagging?",
        answer="Yes, score is 3 months stale.",
        supporting_evidence=["score_date = 2024-01-15", "3 missed payments since Feb"],
        conclusion="Payment behavior is the more current signal. Risk is higher than bureau suggests.",
    )
    report = ReviewReport(
        resolved=[resolution],
        open_conflicts=[],
        cross_domain_insights=["Bureau lag pattern detected — recommend score refresh."],
        data_requests_made=[{"intent": "bureau score timestamp"}],
    )
    assert len(report.resolved) == 1
    assert report.resolved[0].pair == ("bureau", "spend_payments")


def test_final_output_with_data_gap():
    gap = DataGap(
        specialist="modeling",
        missing_data="model_scores table empty",
        absence_interpretation="No scoring run may indicate customer below scoring threshold.",
        is_signal=True,
    )
    output = FinalOutput(
        answer="Based on available evidence...",
        resolved_contradictions=[],
        open_conflicts=[],
        data_gaps=[gap],
        blocked_steps=[],
        specialists_consulted=["bureau", "modeling"],
    )
    assert output.data_gaps[0].is_signal is True
    assert "modeling" in output.specialists_consulted


def test_llm_result_success():
    result = LLMResult(status="success", data={"key": "value"}, error=None)
    assert result.status == "success"


def test_llm_result_blocked():
    result = LLMResult(status="blocked", data=None, error="Firewall rejection 403")
    assert result.status == "blocked"
    assert result.error is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_models/test_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'models'`

- [ ] **Step 3: Implement types**

```python
# models/__init__.py
```

```python
# models/types.py
from __future__ import annotations
from pydantic import BaseModel, Field


class DomainSkill(BaseModel):
    name: str
    system_prompt: str
    data_hints: list[str] = Field(default_factory=list)
    interpretation_guide: str = ""
    risk_signals: list[str] = Field(default_factory=list)


class DataRequestResult(BaseModel):
    intent: str
    variables: list[str] = Field(default_factory=list)
    table_hints: list[str] = Field(default_factory=list)
    data: dict | None = None
    unavailable: bool = False
    unavailable_reason: str = ""


class SynthesisResult(BaseModel):
    question: str
    findings: str
    evidence: list[str] = Field(default_factory=list)
    implications: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)


class ReportSection(BaseModel):
    domain: str
    title: str
    key_findings: str
    supporting_evidence: list[str] = Field(default_factory=list)
    risk_implication: str = ""


class AnswerResult(BaseModel):
    domain: str
    question: str
    answer: str
    evidence: list[str] = Field(default_factory=list)


class SpecialistOutput(BaseModel):
    domain: str
    question: str
    mode: str  # "report" or "chat"
    findings: str
    evidence: list[str] = Field(default_factory=list)
    implications: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    raw_data: dict = Field(default_factory=dict)


class Resolution(BaseModel):
    pair: tuple[str, str]
    contradiction: str
    question_raised: str
    answer: str
    supporting_evidence: list[str] = Field(default_factory=list)
    conclusion: str


class Conflict(BaseModel):
    pair: tuple[str, str]
    contradiction: str
    question_raised: str
    reason_unresolved: str
    evidence_from_both: list[str] = Field(default_factory=list)


class ReviewReport(BaseModel):
    resolved: list[Resolution] = Field(default_factory=list)
    open_conflicts: list[Conflict] = Field(default_factory=list)
    cross_domain_insights: list[str] = Field(default_factory=list)
    data_requests_made: list[dict] = Field(default_factory=list)


class DataGap(BaseModel):
    specialist: str
    missing_data: str
    absence_interpretation: str
    is_signal: bool


class BlockedStep(BaseModel):
    specialist: str
    step: str  # which skill step was blocked
    error: str
    attempts: int


class FinalOutput(BaseModel):
    answer: str
    resolved_contradictions: list[Resolution] = Field(default_factory=list)
    open_conflicts: list[Conflict] = Field(default_factory=list)
    data_gaps: list[DataGap] = Field(default_factory=list)
    blocked_steps: list[BlockedStep] = Field(default_factory=list)
    specialists_consulted: list[str] = Field(default_factory=list)


class LLMResult(BaseModel):
    status: str  # "success" or "blocked"
    data: dict | None = None
    error: str | None = None


class StepRecord(BaseModel):
    prompt: str
    message: str
    result: dict | None = None
    attempt: int = 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_models/test_types.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add models/ tests/test_models/
git commit -m "feat: add Pydantic models for all shared types"
```

---

## Task 3: Event Logger

**Files:**
- Create: `log/event_logger.py`
- Create: `log/__init__.py`
- Create: `tests/test_log/__init__.py`
- Create: `tests/test_log/test_event_logger.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_log/test_event_logger.py
import json
import os
import pytest
from log.event_logger import EventLogger


@pytest.fixture
def logger(tmp_path):
    log = EventLogger(session_id="test-session-001", log_dir=str(tmp_path))
    return log


def test_log_creates_file(logger, tmp_path):
    logger.log("session_start", {"pillar": "credit_risk"})
    log_file = tmp_path / "test-session-001.jsonl"
    assert log_file.exists()


def test_log_writes_valid_jsonl(logger, tmp_path):
    logger.log("session_start", {"pillar": "credit_risk"})
    logger.log("orchestrator_dispatch", {"question": "test?", "specialists": ["bureau"]})
    log_file = tmp_path / "test-session-001.jsonl"
    lines = log_file.read_text().strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        event = json.loads(line)
        assert "timestamp" in event
        assert "session_id" in event
        assert "event" in event


def test_log_includes_trace_id(logger, tmp_path):
    logger.set_trace("q-001")
    logger.log("data_request", {"domain": "bureau", "intent": "delinquency count"})
    log_file = tmp_path / "test-session-001.jsonl"
    event = json.loads(log_file.read_text().strip())
    assert event["trace_id"] == "q-001"


def test_log_without_trace_id(logger, tmp_path):
    logger.log("session_start", {"pillar": "credit_risk"})
    log_file = tmp_path / "test-session-001.jsonl"
    event = json.loads(log_file.read_text().strip())
    assert event["trace_id"] is None


def test_multiple_traces(logger, tmp_path):
    logger.set_trace("q-001")
    logger.log("data_request", {"domain": "bureau"})
    logger.set_trace("q-002")
    logger.log("data_request", {"domain": "modeling"})
    log_file = tmp_path / "test-session-001.jsonl"
    lines = log_file.read_text().strip().split("\n")
    assert json.loads(lines[0])["trace_id"] == "q-001"
    assert json.loads(lines[1])["trace_id"] == "q-002"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_log/test_event_logger.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'log'`

- [ ] **Step 3: Implement EventLogger**

```python
# log/__init__.py
```

```python
# log/event_logger.py
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


class EventLogger:
    def __init__(self, session_id: str, log_dir: str = "logs"):
        self.session_id = session_id
        self.log_dir = log_dir
        self._trace_id: str | None = None
        os.makedirs(log_dir, exist_ok=True)
        self._file_path = os.path.join(log_dir, f"{session_id}.jsonl")

    def set_trace(self, trace_id: str) -> None:
        self._trace_id = trace_id

    def clear_trace(self) -> None:
        self._trace_id = None

    def log(self, event_type: str, payload: dict | None = None) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "trace_id": self._trace_id,
            "event": event_type,
            **(payload or {}),
        }
        with open(self._file_path, "a") as f:
            f.write(json.dumps(event) + "\n")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_log/test_event_logger.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add log/ tests/test_log/
git commit -m "feat: add structured JSON event logger"
```

---

## Task 4: Simulated Data — YAML Profiles & Generator

**Files:**
- Create: `config/data_profiles/bureau_full.yaml`
- Create: `config/data_profiles/bureau_trades.yaml`
- Create: `config/data_profiles/txn_monthly.yaml`
- Create: `config/data_profiles/pmts_detail.yaml`
- Create: `config/data_profiles/model_scores.yaml`
- Create: `config/data_profiles/wcc_flags.yaml`
- Create: `config/data_profiles/xbu_summary.yaml`
- Create: `config/data_profiles/cust_tenure.yaml`
- Create: `config/data_profiles/income_dti.yaml`
- Create: `data/generator.py`
- Create: `tests/test_data/__init__.py`
- Create: `tests/test_data/test_generator.py`

- [ ] **Step 1: Create bureau_full.yaml profile**

```yaml
# config/data_profiles/bureau_full.yaml
table: bureau_full
description: "Full bureau credit file — one row per case, aggregated bureau snapshot"
grain: one_row_per_case
row_count: 50

columns:
  case_id:
    type: string
    format: "CASE-{seq:05d}"
    description: "Unique case identifier"

  score:
    type: int
    range: [300, 850]
    distribution: normal
    mean: 680
    std: 75
    description: "Credit score — FICO-like"

  derog_count:
    type: int
    range: [0, 12]
    distribution: poisson
    lambda: 1.2
    description: "Number of derogatory marks"

  tradeline_ct:
    type: int
    range: [1, 40]
    distribution: normal
    mean: 12
    std: 5
    description: "Active tradelines"

  inquiry_ct:
    type: int
    range: [0, 15]
    distribution: poisson
    lambda: 2.0
    description: "Recent hard inquiries (last 12 months)"

  score_date:
    type: date
    range: ["2024-01-01", "2024-12-31"]
    distribution: uniform
    description: "Date of last bureau score refresh"

correlations:
  - columns: [score, derog_count]
    direction: negative
    strength: 0.7
  - columns: [score, tradeline_ct]
    direction: positive
    strength: 0.3
```

- [ ] **Step 2: Create remaining 8 YAML profiles**

Create each file in `config/data_profiles/`. Follow the same structure as bureau_full.yaml. Key profiles:

`bureau_trades.yaml`:
```yaml
table: bureau_trades
description: "Individual tradeline detail — one row per tradeline per case"
grain: multiple_rows_per_case
rows_per_case: [1, 8]
row_count: 200

columns:
  case_id:
    type: string
    format: "CASE-{seq:05d}"
    description: "Foreign key to case"
  trade_type:
    type: categorical
    categories:
      revolving: 0.45
      installment: 0.35
      mortgage: 0.15
      other: 0.05
    description: "Tradeline type"
  balance:
    type: float
    range: [0, 150000]
    distribution: normal
    mean: 8500
    std: 12000
    description: "Current balance ($)"
  limit:
    type: float
    range: [500, 200000]
    distribution: normal
    mean: 15000
    std: 18000
    description: "Credit limit ($)"
  dpd_status:
    type: categorical
    categories:
      current: 0.70
      "30dpd": 0.12
      "60dpd": 0.08
      "90dpd": 0.06
      "120plus": 0.04
    description: "Days past due bucket"

correlations:
  - columns: [balance, limit]
    direction: positive
    strength: 0.6
```

`txn_monthly.yaml`:
```yaml
table: txn_monthly
description: "Monthly transaction aggregates — one row per month per case"
grain: multiple_rows_per_case
rows_per_case: [6, 18]
row_count: 500

columns:
  case_id:
    type: string
    format: "CASE-{seq:05d}"
    description: "Foreign key to case"
  month:
    type: date
    range: ["2023-01-01", "2024-12-31"]
    distribution: uniform
    description: "Calendar month"
  spend_total:
    type: float
    range: [0, 50000]
    distribution: normal
    mean: 3200
    std: 4500
    description: "Total spend ($)"
  txn_count:
    type: int
    range: [0, 200]
    distribution: normal
    mean: 35
    std: 25
    description: "Transaction count"
  category:
    type: categorical
    categories:
      retail: 0.30
      dining: 0.20
      travel: 0.15
      grocery: 0.15
      services: 0.10
      other: 0.10
    description: "Spend category"
```

`pmts_detail.yaml`:
```yaml
table: pmts_detail
description: "Payment history — one row per billing cycle per case"
grain: multiple_rows_per_case
rows_per_case: [6, 18]
row_count: 500

columns:
  case_id:
    type: string
    format: "CASE-{seq:05d}"
    description: "Foreign key to case"
  due_date:
    type: date
    range: ["2023-01-01", "2024-12-31"]
    distribution: uniform
    description: "Payment due date"
  paid_date:
    type: date
    range: ["2023-01-01", "2025-01-31"]
    distribution: uniform
    description: "Actual payment date"
  amount:
    type: float
    range: [25, 15000]
    distribution: normal
    mean: 1200
    std: 2000
    description: "Payment amount ($)"
  status:
    type: categorical
    categories:
      on_time: 0.72
      late: 0.18
      missed: 0.10
    description: "Payment status"
```

`model_scores.yaml`:
```yaml
table: model_scores
description: "Internal model outputs — one row per model per scoring run"
grain: multiple_rows_per_case
rows_per_case: [1, 4]
row_count: 100

columns:
  case_id:
    type: string
    format: "CASE-{seq:05d}"
    description: "Foreign key to case"
  model_id:
    type: categorical
    categories:
      risk_v3: 0.40
      propensity_v2: 0.30
      collections_v1: 0.30
    description: "Model identifier"
  score:
    type: float
    range: [0.0, 1.0]
    distribution: normal
    mean: 0.45
    std: 0.2
    description: "Raw score value (probability)"
  percentile:
    type: int
    range: [1, 100]
    distribution: uniform
    description: "Population percentile"
  timestamp:
    type: date
    range: ["2024-01-01", "2024-12-31"]
    distribution: uniform
    description: "Scoring date"
```

`wcc_flags.yaml`:
```yaml
table: wcc_flags
description: "Watch-list / credit control flags — event-level, one row per flag"
grain: multiple_rows_per_case
rows_per_case: [0, 3]
row_count: 60

columns:
  case_id:
    type: string
    format: "CASE-{seq:05d}"
    description: "Foreign key to case"
  flag_type:
    type: categorical
    categories:
      overlimit: 0.25
      rapid_spend: 0.20
      payment_default: 0.20
      fraud_alert: 0.15
      manual_review: 0.20
    description: "Flag category"
  trigger_date:
    type: date
    range: ["2024-01-01", "2024-12-31"]
    distribution: uniform
    description: "Date flag raised"
  severity:
    type: categorical
    categories:
      low: 0.30
      medium: 0.35
      high: 0.25
      critical: 0.10
    description: "Severity level"
```

`xbu_summary.yaml`:
```yaml
table: xbu_summary
description: "Cross-BU exposure — one row per product per case"
grain: multiple_rows_per_case
rows_per_case: [1, 4]
row_count: 120

columns:
  case_id:
    type: string
    format: "CASE-{seq:05d}"
    description: "Foreign key to case"
  product:
    type: categorical
    categories:
      credit_card: 0.40
      charge_card: 0.25
      personal_loan: 0.20
      savings: 0.15
    description: "Product name"
  exposure:
    type: float
    range: [0, 100000]
    distribution: normal
    mean: 12000
    std: 15000
    description: "Total exposure ($)"
  utilization:
    type: float
    range: [0.0, 1.5]
    distribution: normal
    mean: 0.45
    std: 0.25
    description: "Limit utilization ratio"
```

`cust_tenure.yaml`:
```yaml
table: cust_tenure
description: "Customer relationship — one row per case"
grain: one_row_per_case
row_count: 50

columns:
  case_id:
    type: string
    format: "CASE-{seq:05d}"
    description: "Unique case identifier"
  tenure_months:
    type: int
    range: [1, 360]
    distribution: normal
    mean: 84
    std: 60
    description: "Months as customer"
  products_held:
    type: int
    range: [1, 6]
    distribution: poisson
    lambda: 2.0
    description: "Active product count"
  segment:
    type: categorical
    categories:
      mass: 0.45
      affluent: 0.30
      high_net_worth: 0.15
      ultra_hnw: 0.10
    description: "Customer segment"
```

`income_dti.yaml`:
```yaml
table: income_dti
description: "Income and capacity — one row per case"
grain: one_row_per_case
row_count: 50

columns:
  case_id:
    type: string
    format: "CASE-{seq:05d}"
    description: "Unique case identifier"
  income_est:
    type: float
    range: [20000, 500000]
    distribution: normal
    mean: 75000
    std: 45000
    description: "Estimated annual income ($)"
  total_debt:
    type: float
    range: [0, 300000]
    distribution: normal
    mean: 35000
    std: 40000
    description: "Total debt obligations ($)"
  dti_ratio:
    type: float
    range: [0.0, 1.5]
    distribution: normal
    mean: 0.35
    std: 0.15
    description: "Debt-to-income ratio"

correlations:
  - columns: [income_est, total_debt]
    direction: positive
    strength: 0.5
  - columns: [total_debt, dti_ratio]
    direction: positive
    strength: 0.8
```

- [ ] **Step 3: Write tests for generator**

```python
# tests/test_data/test_generator.py
import pytest
import yaml
import os
from data.generator import DataGenerator


@pytest.fixture
def sample_profile(tmp_path):
    profile = {
        "table": "test_table",
        "description": "Test table",
        "grain": "one_row_per_case",
        "row_count": 20,
        "columns": {
            "case_id": {"type": "string", "format": "CASE-{seq:05d}"},
            "score": {
                "type": "int", "range": [300, 850],
                "distribution": "normal", "mean": 680, "std": 75,
            },
            "status": {
                "type": "categorical",
                "categories": {"active": 0.7, "closed": 0.3},
            },
        },
    }
    path = tmp_path / "test_table.yaml"
    with open(path, "w") as f:
        yaml.dump(profile, f)
    return str(tmp_path)


def test_generator_loads_profiles(sample_profile):
    gen = DataGenerator(profile_dir=sample_profile, seed=42)
    assert "test_table" in gen.profiles


def test_generator_produces_correct_row_count(sample_profile):
    gen = DataGenerator(profile_dir=sample_profile, seed=42)
    tables = gen.generate()
    assert len(tables["test_table"]) == 20


def test_generator_respects_int_range(sample_profile):
    gen = DataGenerator(profile_dir=sample_profile, seed=42)
    tables = gen.generate()
    for row in tables["test_table"]:
        assert 300 <= row["score"] <= 850


def test_generator_categorical_values(sample_profile):
    gen = DataGenerator(profile_dir=sample_profile, seed=42)
    tables = gen.generate()
    valid = {"active", "closed"}
    for row in tables["test_table"]:
        assert row["status"] in valid


def test_generator_sequential_case_ids(sample_profile):
    gen = DataGenerator(profile_dir=sample_profile, seed=42)
    tables = gen.generate()
    assert tables["test_table"][0]["case_id"] == "CASE-00001"
    assert tables["test_table"][19]["case_id"] == "CASE-00020"


def test_generator_deterministic_with_seed(sample_profile):
    gen1 = DataGenerator(profile_dir=sample_profile, seed=42)
    gen2 = DataGenerator(profile_dir=sample_profile, seed=42)
    t1 = gen1.generate()
    t2 = gen2.generate()
    assert t1["test_table"] == t2["test_table"]


def test_generator_row_count_override(sample_profile):
    gen = DataGenerator(profile_dir=sample_profile, seed=42)
    tables = gen.generate(row_count_override=5)
    assert len(tables["test_table"]) == 5


def test_generator_dumps_csv(sample_profile, tmp_path):
    gen = DataGenerator(profile_dir=sample_profile, seed=42)
    tables = gen.generate()
    output_dir = tmp_path / "output"
    gen.dump_csv(tables, str(output_dir))
    csv_path = output_dir / "test_table.csv"
    assert csv_path.exists()
    lines = csv_path.read_text().strip().split("\n")
    assert len(lines) == 21  # header + 20 rows
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `pytest tests/test_data/test_generator.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 5: Implement generator**

```python
# data/generator.py
from __future__ import annotations

import csv
import os
import random
from datetime import date, timedelta

import numpy as np
import yaml


class DataGenerator:
    def __init__(self, profile_dir: str = "config/data_profiles", seed: int | None = None):
        self.profile_dir = profile_dir
        self.profiles: dict[str, dict] = {}
        self._load_profiles()
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)

    def _load_profiles(self) -> None:
        for fname in os.listdir(self.profile_dir):
            if fname.endswith(".yaml") or fname.endswith(".yml"):
                with open(os.path.join(self.profile_dir, fname)) as f:
                    profile = yaml.safe_load(f)
                self.profiles[profile["table"]] = profile

    def generate(self, row_count_override: int | None = None) -> dict[str, list[dict]]:
        tables: dict[str, list[dict]] = {}
        for table_name, profile in self.profiles.items():
            n = row_count_override or profile.get("row_count", 50)
            tables[table_name] = self._generate_table(profile, n)
        return tables

    def _generate_table(self, profile: dict, n: int) -> list[dict]:
        columns = profile["columns"]
        rows: list[dict] = []
        for i in range(n):
            row: dict = {}
            for col_name, col_def in columns.items():
                row[col_name] = self._generate_value(col_def, i + 1)
            rows.append(row)

        # Apply correlations if defined
        correlations = profile.get("correlations", [])
        if correlations:
            self._apply_correlations(rows, columns, correlations)

        return rows

    def _generate_value(self, col_def: dict, seq: int):
        col_type = col_def["type"]

        if col_type == "string":
            fmt = col_def.get("format", "")
            return fmt.format(seq=seq)

        if col_type == "int":
            return self._generate_numeric_int(col_def)

        if col_type == "float":
            return self._generate_numeric_float(col_def)

        if col_type == "categorical":
            categories = col_def["categories"]
            labels = list(categories.keys())
            weights = list(categories.values())
            return random.choices(labels, weights=weights, k=1)[0]

        if col_type == "date":
            r = col_def.get("range", ["2024-01-01", "2024-12-31"])
            start = date.fromisoformat(r[0])
            end = date.fromisoformat(r[1])
            delta = (end - start).days
            offset = random.randint(0, max(delta, 0))
            return (start + timedelta(days=offset)).isoformat()

        return None

    def _generate_numeric_int(self, col_def: dict) -> int:
        lo, hi = col_def.get("range", [0, 100])
        dist = col_def.get("distribution", "uniform")
        if dist == "normal":
            val = np.random.normal(col_def.get("mean", (lo + hi) / 2), col_def.get("std", (hi - lo) / 6))
        elif dist == "poisson":
            val = np.random.poisson(col_def.get("lambda", 1.0))
        else:
            val = random.randint(lo, hi)
        return int(np.clip(val, lo, hi))

    def _generate_numeric_float(self, col_def: dict) -> float:
        lo, hi = col_def.get("range", [0.0, 1.0])
        dist = col_def.get("distribution", "uniform")
        if dist == "normal":
            val = np.random.normal(col_def.get("mean", (lo + hi) / 2), col_def.get("std", (hi - lo) / 6))
        else:
            val = random.uniform(lo, hi)
        return round(float(np.clip(val, lo, hi)), 2)

    def _apply_correlations(self, rows: list[dict], columns: dict, correlations: list[dict]) -> None:
        """Simple rank-based correlation: sort one column to correlate with another."""
        for corr in correlations:
            cols = corr["columns"]
            if len(cols) != 2:
                continue
            col_a, col_b = cols[0], cols[1]
            if col_a not in columns or col_b not in columns:
                continue
            if columns[col_a]["type"] not in ("int", "float"):
                continue
            if columns[col_b]["type"] not in ("int", "float"):
                continue

            direction = corr.get("direction", "positive")
            strength = corr.get("strength", 0.5)

            # Sort rows by col_a, then assign col_b values in correlated order
            sorted_b_values = sorted([r[col_b] for r in rows], reverse=(direction == "negative"))
            a_ranked = sorted(range(len(rows)), key=lambda i: rows[i][col_a])

            for rank, idx in enumerate(a_ranked):
                # Blend: strength * correlated_value + (1 - strength) * original_value
                original = rows[idx][col_b]
                correlated = sorted_b_values[rank]
                blended = strength * correlated + (1 - strength) * original
                if columns[col_b]["type"] == "int":
                    lo, hi = columns[col_b].get("range", [0, 100])
                    rows[idx][col_b] = int(np.clip(round(blended), lo, hi))
                else:
                    lo, hi = columns[col_b].get("range", [0.0, 1.0])
                    rows[idx][col_b] = round(float(np.clip(blended, lo, hi)), 2)

    def dump_csv(self, tables: dict[str, list[dict]], output_dir: str) -> None:
        os.makedirs(output_dir, exist_ok=True)
        for table_name, rows in tables.items():
            if not rows:
                continue
            path = os.path.join(output_dir, f"{table_name}.csv")
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_data/test_generator.py -v`
Expected: All 8 tests PASS

- [ ] **Step 7: Commit**

```bash
git add config/data_profiles/ data/generator.py tests/test_data/
git commit -m "feat: add YAML data profiles and simulated data generator"
```

---

## Task 5: Data Gateway, Catalog & Tools

**Files:**
- Create: `data/catalog.py`
- Create: `data/gateway.py`
- Create: `data/access_control.py`
- Create: `tools/data_tools.py`
- Create: `tests/test_data/test_gateway.py`
- Create: `tests/test_tools/__init__.py`
- Create: `tests/test_tools/test_data_tools.py`

- [ ] **Step 1: Write tests for catalog and gateway**

```python
# tests/test_data/test_gateway.py
import pytest
from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway


@pytest.fixture
def sample_tables():
    return {
        "bureau_full": [
            {"case_id": "CASE-00001", "score": 720, "derog_count": 0},
            {"case_id": "CASE-00002", "score": 580, "derog_count": 4},
        ],
        "pmts_detail": [
            {"case_id": "CASE-00001", "status": "on_time", "amount": 500},
            {"case_id": "CASE-00001", "status": "late", "amount": 200},
            {"case_id": "CASE-00002", "status": "missed", "amount": 0},
        ],
    }


@pytest.fixture
def catalog():
    return DataCatalog(profile_dir="config/data_profiles")


@pytest.fixture
def gateway(sample_tables):
    return SimulatedDataGateway(tables=sample_tables)


def test_catalog_lists_tables(catalog):
    tables = catalog.list_tables()
    assert "bureau_full" in tables


def test_catalog_get_schema(catalog):
    schema = catalog.get_schema("bureau_full")
    assert "score" in schema
    assert schema["score"]["type"] == "int"


def test_catalog_get_schema_missing_table(catalog):
    schema = catalog.get_schema("nonexistent")
    assert schema is None


def test_gateway_query_all(gateway):
    rows = gateway.query("bureau_full")
    assert len(rows) == 2


def test_gateway_query_with_filter(gateway):
    rows = gateway.query("bureau_full", filters={"case_id": "CASE-00001"})
    assert len(rows) == 1
    assert rows[0]["score"] == 720


def test_gateway_query_missing_table(gateway):
    rows = gateway.query("nonexistent")
    assert rows is None


def test_gateway_query_multi_row(gateway):
    rows = gateway.query("pmts_detail", filters={"case_id": "CASE-00001"})
    assert len(rows) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_data/test_gateway.py -v`
Expected: FAIL

- [ ] **Step 3: Implement catalog**

```python
# data/catalog.py
from __future__ import annotations

import os
import yaml


class DataCatalog:
    """Registry of available tables and their schemas, loaded from YAML profiles."""

    def __init__(self, profile_dir: str = "config/data_profiles"):
        self._schemas: dict[str, dict] = {}
        self._descriptions: dict[str, str] = {}
        self._load(profile_dir)

    def _load(self, profile_dir: str) -> None:
        if not os.path.isdir(profile_dir):
            return
        for fname in os.listdir(profile_dir):
            if not (fname.endswith(".yaml") or fname.endswith(".yml")):
                continue
            with open(os.path.join(profile_dir, fname)) as f:
                profile = yaml.safe_load(f)
            table_name = profile["table"]
            self._descriptions[table_name] = profile.get("description", "")
            self._schemas[table_name] = {
                col_name: {
                    "type": col_def.get("type", "string"),
                    "description": col_def.get("description", ""),
                }
                for col_name, col_def in profile.get("columns", {}).items()
            }

    def list_tables(self) -> list[str]:
        return list(self._schemas.keys())

    def get_schema(self, table_name: str) -> dict | None:
        return self._schemas.get(table_name)

    def get_description(self, table_name: str) -> str:
        return self._descriptions.get(table_name, "")

    def to_prompt_context(self) -> str:
        """Format catalog as text for injection into LLM prompts."""
        lines = ["Available data tables:\n"]
        for table in sorted(self._schemas):
            lines.append(f"  {table}: {self._descriptions.get(table, '')}")
            for col, info in self._schemas[table].items():
                lines.append(f"    - {col} ({info['type']}): {info['description']}")
        return "\n".join(lines)
```

- [ ] **Step 4: Implement gateway**

```python
# data/gateway.py
from __future__ import annotations

from abc import ABC, abstractmethod


class DataGateway(ABC):
    @abstractmethod
    def query(self, table: str, filters: dict | None = None,
              limit: int = 100) -> list[dict] | None:
        """Query a table. Returns None if table doesn't exist."""
        ...

    @abstractmethod
    def list_tables(self) -> list[str]:
        ...


class SimulatedDataGateway(DataGateway):
    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self._tables: dict[str, list[dict]] = tables or {}

    def load_tables(self, tables: dict[str, list[dict]]) -> None:
        self._tables = tables

    def query(self, table: str, filters: dict | None = None,
              limit: int = 100) -> list[dict] | None:
        rows = self._tables.get(table)
        if rows is None:
            return None
        if filters:
            rows = [
                r for r in rows
                if all(r.get(k) == v for k, v in filters.items())
            ]
        return rows[:limit]

    def list_tables(self) -> list[str]:
        return list(self._tables.keys())
```

- [ ] **Step 5: Implement access control (stub for POC)**

```python
# data/access_control.py
from __future__ import annotations


class PillarAccessControl:
    """Column-level access control per pillar. 
    Configured via pillar YAML silenced_tables / silenced_schemas."""

    def __init__(self, silenced_tables: list[str] | None = None,
                 silenced_columns: dict[str, list[str]] | None = None):
        self.silenced_tables = set(silenced_tables or [])
        self.silenced_columns = silenced_columns or {}

    def filter_row(self, table: str, row: dict) -> dict | None:
        if table in self.silenced_tables:
            return None
        blocked_cols = set(self.silenced_columns.get(table, []))
        if not blocked_cols:
            return row
        return {k: v for k, v in row.items() if k not in blocked_cols}

    def is_table_allowed(self, table: str) -> bool:
        return table not in self.silenced_tables
```

- [ ] **Step 6: Implement data tools**

```python
# tools/data_tools.py
from __future__ import annotations

from data.catalog import DataCatalog
from data.gateway import DataGateway

# Module-level references — set by main.py at startup
_gateway: DataGateway | None = None
_catalog: DataCatalog | None = None


def init_tools(gateway: DataGateway, catalog: DataCatalog) -> None:
    global _gateway, _catalog
    _gateway = gateway
    _catalog = catalog


def list_available_tables() -> str:
    """List all available data tables and their descriptions."""
    if _catalog is None:
        return "Error: data catalog not initialized"
    tables = _catalog.list_tables()
    lines = []
    for t in sorted(tables):
        desc = _catalog.get_description(t)
        lines.append(f"{t}: {desc}")
    return "\n".join(lines) if lines else "No tables available."


def get_table_schema(table_name: str) -> str:
    """Get the column schema for a specific table."""
    if _catalog is None:
        return "Error: data catalog not initialized"
    schema = _catalog.get_schema(table_name)
    if schema is None:
        return f"Table '{table_name}' not found in catalog."
    lines = [f"Schema for {table_name}:"]
    for col, info in schema.items():
        lines.append(f"  {col} ({info['type']}): {info['description']}")
    return "\n".join(lines)


def query_table(table_name: str, filter_column: str = "",
                filter_value: str = "", limit: int = 50) -> str:
    """Query a data table with optional filter."""
    if _gateway is None:
        return "Error: data gateway not initialized"
    filters = {}
    if filter_column and filter_value:
        filters[filter_column] = filter_value
    rows = _gateway.query(table_name, filters=filters, limit=limit)
    if rows is None:
        return f"Data unavailable: table '{table_name}' not found."
    if not rows:
        return f"No rows matching filter in '{table_name}'."
    # Truncate large results
    import json
    result = json.dumps(rows, default=str)
    if len(result) > 3000:
        result = result[:3000] + "\n[truncated]"
    return result
```

- [ ] **Step 7: Write tool tests**

```python
# tests/test_tools/test_data_tools.py
import pytest
from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway
from tools.data_tools import init_tools, list_available_tables, get_table_schema, query_table


@pytest.fixture(autouse=True)
def setup_tools():
    tables = {
        "bureau_full": [
            {"case_id": "CASE-00001", "score": 720, "derog_count": 0},
            {"case_id": "CASE-00002", "score": 580, "derog_count": 4},
        ],
    }
    gw = SimulatedDataGateway(tables=tables)
    cat = DataCatalog(profile_dir="config/data_profiles")
    init_tools(gw, cat)


def test_list_tables():
    result = list_available_tables()
    assert "bureau_full" in result


def test_get_schema():
    result = get_table_schema("bureau_full")
    assert "score" in result


def test_get_schema_missing():
    result = get_table_schema("nonexistent")
    assert "not found" in result


def test_query_table_all():
    result = query_table("bureau_full")
    assert "CASE-00001" in result


def test_query_table_filtered():
    result = query_table("bureau_full", filter_column="case_id", filter_value="CASE-00002")
    assert "580" in result
    assert "CASE-00001" not in result


def test_query_table_missing():
    result = query_table("nonexistent")
    assert "unavailable" in result.lower() or "not found" in result.lower()
```

- [ ] **Step 8: Run all tests**

Run: `pytest tests/test_data/ tests/test_tools/ -v`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add data/ tools/ tests/test_data/ tests/test_tools/
git commit -m "feat: add data catalog, simulated gateway, access control, and data tools"
```

---

## Task 6: LLM Gateway — Adapter ABC & OpenAI Adapter

**Files:**
- Create: `gateway/llm_adapter.py`
- Create: `gateway/openai_adapter.py`
- Create: `tests/test_gateway/__init__.py`
- Create: `tests/test_gateway/test_llm_adapter.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_gateway/test_llm_adapter.py
import pytest
from unittest.mock import MagicMock, patch
from pydantic import BaseModel
from gateway.llm_adapter import BaseLLMAdapter
from gateway.openai_adapter import OpenAIAdapter


def test_base_adapter_is_abstract():
    with pytest.raises(TypeError):
        BaseLLMAdapter()


class MockOutput(BaseModel):
    answer: str
    confidence: float


def test_openai_adapter_instantiation():
    """Test adapter can be created (actual API calls tested in integration)."""
    with patch("gateway.openai_adapter.OpenAI"):
        adapter = OpenAIAdapter(model="gpt-4.1")
        assert adapter.model == "gpt-4.1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gateway/test_llm_adapter.py -v`
Expected: FAIL

- [ ] **Step 3: Implement adapter ABC**

```python
# gateway/llm_adapter.py
from __future__ import annotations

from abc import ABC, abstractmethod
from pydantic import BaseModel


class BaseLLMAdapter(ABC):
    @abstractmethod
    def run(self, system_prompt: str, user_message: str,
            tools: list | None = None,
            output_type: type[BaseModel] | None = None,
            max_turns: int = 12) -> dict:
        """Full agent loop — tool calling until final structured output.
        Returns dict (parsed from output_type if provided, or raw response)."""
        ...

    @abstractmethod
    def chat_turn(self, messages: list[dict]) -> str:
        """Single conversational turn. Returns string response."""
        ...
```

- [ ] **Step 4: Implement OpenAI adapter**

```python
# gateway/openai_adapter.py
from __future__ import annotations

import json
import os

from openai import OpenAI
from pydantic import BaseModel

from gateway.llm_adapter import BaseLLMAdapter


class OpenAIAdapter(BaseLLMAdapter):
    def __init__(self, model: str = "gpt-4.1", api_key: str | None = None):
        self.model = model
        self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))

    def run(self, system_prompt: str, user_message: str,
            tools: list | None = None,
            output_type: type[BaseModel] | None = None,
            max_turns: int = 12) -> dict:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # Build tool definitions for OpenAI function calling
        tool_defs = self._build_tool_defs(tools) if tools else None
        tool_map = {t.__name__: t for t in (tools or [])}

        for _ in range(max_turns):
            kwargs: dict = {"model": self.model, "messages": messages}
            if tool_defs:
                kwargs["tools"] = tool_defs

            response = self.client.chat.completions.create(**kwargs)
            choice = response.choices[0]

            # If model wants to call a tool
            if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                messages.append(choice.message)
                for tool_call in choice.message.tool_calls:
                    fn_name = tool_call.function.name
                    fn_args = json.loads(tool_call.function.arguments)
                    fn = tool_map.get(fn_name)
                    if fn:
                        result = fn(**fn_args)
                    else:
                        result = f"Error: unknown tool '{fn_name}'"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": str(result),
                    })
                continue

            # Final response
            content = choice.message.content or ""
            if output_type:
                try:
                    parsed = json.loads(content)
                    return parsed
                except json.JSONDecodeError:
                    return {"raw": content}
            return {"raw": content}

        return {"error": "max_turns exceeded"}

    def chat_turn(self, messages: list[dict]) -> str:
        response = self.client.chat.completions.create(
            model=self.model, messages=messages,
        )
        return response.choices[0].message.content or ""

    def _build_tool_defs(self, tools: list) -> list[dict]:
        """Convert Python functions to OpenAI tool format."""
        defs = []
        for fn in tools:
            import inspect
            sig = inspect.signature(fn)
            params = {}
            for name, param in sig.parameters.items():
                annotation = param.annotation
                if annotation == int:
                    params[name] = {"type": "integer"}
                elif annotation == float:
                    params[name] = {"type": "number"}
                else:
                    params[name] = {"type": "string"}
                if param.default != inspect.Parameter.empty:
                    params[name]["default"] = param.default

            defs.append({
                "type": "function",
                "function": {
                    "name": fn.__name__,
                    "description": fn.__doc__ or "",
                    "parameters": {
                        "type": "object",
                        "properties": params,
                        "required": [
                            n for n, p in sig.parameters.items()
                            if p.default == inspect.Parameter.empty
                        ],
                    },
                },
            })
        return defs
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_gateway/test_llm_adapter.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add gateway/llm_adapter.py gateway/openai_adapter.py tests/test_gateway/
git commit -m "feat: add LLM adapter ABC and OpenAI adapter for local dev"
```

---

## Task 7: Firewall Retry Stack

**Files:**
- Create: `gateway/firewall_stack.py`
- Create: `tests/test_gateway/test_firewall_stack.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_gateway/test_firewall_stack.py
import pytest
from unittest.mock import MagicMock
from gateway.firewall_stack import FirewallStack, FirewallRejection
from log.event_logger import EventLogger


@pytest.fixture
def mock_adapter():
    return MagicMock()


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test", log_dir=str(tmp_path))


@pytest.fixture
def stack(mock_adapter, logger):
    return FirewallStack(adapter=mock_adapter, logger=logger, max_retries=2)


def test_successful_call(stack, mock_adapter):
    mock_adapter.run.return_value = {"answer": "42"}
    result = stack.call("prompt", "message", tools=[], output_type=None)
    assert result.status == "success"
    assert result.data == {"answer": "42"}
    assert len(stack.step_history) == 1


def test_retry_on_firewall_rejection(stack, mock_adapter):
    mock_adapter.run.side_effect = [
        FirewallRejection(code=403, message="blocked content"),
        {"answer": "42"},
    ]
    result = stack.call("prompt", "message", tools=[], output_type=None)
    assert result.status == "success"
    assert mock_adapter.run.call_count == 2


def test_exhausted_retries(stack, mock_adapter):
    mock_adapter.run.side_effect = FirewallRejection(code=403, message="blocked")
    result = stack.call("prompt", "message", tools=[], output_type=None)
    assert result.status == "blocked"
    assert "blocked" in result.error


def test_step_history_tracks_success(stack, mock_adapter):
    mock_adapter.run.return_value = {"answer": "1"}
    stack.call("p1", "m1", tools=[], output_type=None)
    mock_adapter.run.return_value = {"answer": "2"}
    stack.call("p2", "m2", tools=[], output_type=None)
    assert len(stack.step_history) == 2


def test_rollback(stack, mock_adapter):
    mock_adapter.run.return_value = {"answer": "1"}
    stack.call("p1", "m1", tools=[], output_type=None)
    stack.call("p2", "m2", tools=[], output_type=None)
    stack.call("p3", "m3", tools=[], output_type=None)
    stack.rollback_to(1)
    assert len(stack.step_history) == 1


def test_firewall_guidance_added_on_retry(stack, mock_adapter):
    """Verify the retry call includes firewall guidance in the prompt."""
    calls = []
    def capture_run(system_prompt, user_message, **kwargs):
        calls.append(system_prompt)
        if len(calls) == 1:
            raise FirewallRejection(code=403, message="raw numbers detected")
        return {"answer": "safe response"}

    mock_adapter.run.side_effect = capture_run
    stack.call("original prompt", "message", tools=[], output_type=None)
    assert len(calls) == 2
    assert "previous response was blocked" in calls[1].lower() or "avoid" in calls[1].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gateway/test_firewall_stack.py -v`
Expected: FAIL

- [ ] **Step 3: Implement FirewallStack**

```python
# gateway/firewall_stack.py
from __future__ import annotations

from dataclasses import dataclass, field

from gateway.llm_adapter import BaseLLMAdapter
from log.event_logger import EventLogger
from models.types import LLMResult, StepRecord


class FirewallRejection(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Firewall rejection {code}: {message}")


FIREWALL_GUIDANCE = (
    "\n\n[IMPORTANT: Your previous response was blocked by the content firewall. "
    "Avoid: raw account numbers, PII, role-injection patterns like [SYSTEM] or [USER], "
    "code execution keywords (exec, eval, import). Use masked identifiers (e.g. acc_***XX) "
    "and descriptive language instead of raw numeric values.]"
)


class FirewallStack:
    def __init__(self, adapter: BaseLLMAdapter, logger: EventLogger,
                 max_retries: int = 2):
        self.adapter = adapter
        self.logger = logger
        self.max_retries = max_retries
        self.step_history: list[StepRecord] = []

    def call(self, system_prompt: str, user_message: str,
             tools: list | None = None,
             output_type=None) -> LLMResult:
        attempt = 0
        current_prompt = system_prompt
        current_message = user_message

        while attempt <= self.max_retries:
            try:
                result = self.adapter.run(
                    system_prompt=current_prompt,
                    user_message=current_message,
                    tools=tools,
                    output_type=output_type,
                )
                self.step_history.append(StepRecord(
                    prompt=current_prompt,
                    message=current_message,
                    result=result if isinstance(result, dict) else {"raw": str(result)},
                    attempt=attempt,
                ))
                return LLMResult(
                    status="success",
                    data=result if isinstance(result, dict) else {"raw": str(result)},
                )

            except FirewallRejection as e:
                attempt += 1
                self.logger.log("firewall_rejection", {
                    "attempt": attempt,
                    "error_code": e.code,
                    "error_message": e.message,
                    "step_index": len(self.step_history),
                })

                if attempt > self.max_retries:
                    self.logger.log("firewall_blocked", {
                        "total_attempts": attempt,
                        "step_index": len(self.step_history),
                    })
                    return LLMResult(status="blocked", error=str(e))

                self.logger.log("firewall_retry", {
                    "attempt": attempt,
                    "step_index": len(self.step_history),
                })
                current_prompt = current_prompt + FIREWALL_GUIDANCE
                current_message = self._sanitize_message(current_message)

        return LLMResult(status="blocked", error="max retries exceeded")

    def rollback_to(self, step_index: int) -> None:
        self.step_history = self.step_history[:step_index]

    def _sanitize_message(self, message: str) -> str:
        """Basic sanitization — mask obvious numeric patterns."""
        import re
        # Mask long digit sequences (potential account numbers)
        sanitized = re.sub(r'\b\d{8,}\b', '***MASKED***', message)
        return sanitized
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_gateway/test_firewall_stack.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/firewall_stack.py tests/test_gateway/test_firewall_stack.py
git commit -m "feat: add firewall retry stack with rollback"
```

---

## Task 8: Pillar YAML Config Loader

**Files:**
- Create: `config/pillars/credit_risk.yaml`
- Create: `config/pillars/escalation.yaml`
- Create: `config/pillars/cbo.yaml`
- Create: `config/pillar_loader.py`
- Create: `tests/test_config/__init__.py`
- Create: `tests/test_config/test_pillar_loader.py`

- [ ] **Step 1: Create pillar YAML files**

```yaml
# config/pillars/credit_risk.yaml
pillar: credit_risk
display_name: "Credit & Risk"
focus: "Delinquency risk, bureau scores, cross-BU exposure, model scores, spend patterns, WCC flags, capacity & affordability"

specialists:
  bureau:
    focus: "Delinquency Risk"
    prompt_overlay: "Flag 90D+ Marks"
  crossbu:
    focus: "Cross-BU Exposure"
    prompt_overlay: "Multi-product overlap"
  modeling:
    focus: "Score Trajectories"
    prompt_overlay: "Trend vs threshold"
  spend_payments:
    focus: "Payment Behavior"
    prompt_overlay: "6M rolling trends"
  wcc:
    focus: "Watch-list Flags"
    prompt_overlay: "Standalone signals"
  customer_rel:
    focus: "Relationship Depth"
    prompt_overlay: "Product breadth"
  capacity_afford:
    focus: "Capacity Headroom"
    prompt_overlay: "Limit vs DTI"

silenced_tables: []
silenced_columns: {}
```

```yaml
# config/pillars/escalation.yaml
pillar: escalation
display_name: "Escalation"
focus: "Escalation triggers, cross-product risk, payment behavior alongside risk scores, WCC flags, capacity headroom assessment"

specialists:
  bureau:
    focus: "Escalation Signals"
    prompt_overlay: "Recent derog triggers"
  crossbu:
    focus: "Cross-Product Risk"
    prompt_overlay: "Product contagion"
  modeling:
    focus: "Risk Score Context"
    prompt_overlay: "Score vs escalation"
  spend_payments:
    focus: "Payment Behavior"
    prompt_overlay: "Delinquency trajectory"
  wcc:
    focus: "Escalation Triggers"
    prompt_overlay: "Trigger classification"
  customer_rel:
    focus: "Relationship Context"
    prompt_overlay: "Tenure vs escalation"
  capacity_afford:
    focus: "Headroom Assessment"
    prompt_overlay: "Absorb capacity"

silenced_tables: []
silenced_columns: {}
```

```yaml
# config/pillars/cbo.yaml
pillar: cbo
display_name: "CBO — Credit Burst Out"
focus: "Limit utilization, burst spending signals, concentrated cross-BU risk, limit pressure vs WCC behavior"

specialists:
  bureau:
    focus: "Limit Utilisation"
    prompt_overlay: "Burst Risk Signals"
  crossbu:
    focus: "Concentrated Risk"
    prompt_overlay: "Cross-BU concentration"
  modeling:
    focus: "Burst Score Signals"
    prompt_overlay: "Burst vs baseline"
  spend_payments:
    focus: "Burst Spending"
    prompt_overlay: "Spike detection"
  wcc:
    focus: "Limit Pressure"
    prompt_overlay: "Limit vs WCC alignment"
  customer_rel:
    focus: "Usage Patterns"
    prompt_overlay: "Product burst signals"
  capacity_afford:
    focus: "Limit Exposure"
    prompt_overlay: "Utilization headroom"

silenced_tables: []
silenced_columns: {}
```

- [ ] **Step 2: Write tests for pillar loader**

```python
# tests/test_config/test_pillar_loader.py
import pytest
from config.pillar_loader import PillarLoader


@pytest.fixture
def loader():
    return PillarLoader(pillar_dir="config/pillars")


def test_load_credit_risk(loader):
    config = loader.load("credit_risk")
    assert config["pillar"] == "credit_risk"
    assert "bureau" in config["specialists"]


def test_specialist_has_focus_and_overlay(loader):
    config = loader.load("credit_risk")
    bureau = config["specialists"]["bureau"]
    assert bureau["focus"] == "Delinquency Risk"
    assert bureau["prompt_overlay"] == "Flag 90D+ Marks"


def test_load_all_pillars(loader):
    pillars = loader.list_pillars()
    assert "credit_risk" in pillars
    assert "escalation" in pillars
    assert "cbo" in pillars


def test_load_nonexistent_pillar(loader):
    config = loader.load("nonexistent")
    assert config is None


def test_get_specialist_config(loader):
    spec_config = loader.get_specialist_config("credit_risk", "bureau")
    assert spec_config["focus"] == "Delinquency Risk"


def test_get_specialist_config_missing(loader):
    spec_config = loader.get_specialist_config("credit_risk", "nonexistent")
    assert spec_config is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_config/test_pillar_loader.py -v`
Expected: FAIL

- [ ] **Step 4: Implement pillar loader**

```python
# config/pillar_loader.py
from __future__ import annotations

import os
import yaml


class PillarLoader:
    def __init__(self, pillar_dir: str = "config/pillars"):
        self.pillar_dir = pillar_dir
        self._cache: dict[str, dict] = {}

    def load(self, pillar_name: str) -> dict | None:
        if pillar_name in self._cache:
            return self._cache[pillar_name]
        path = os.path.join(self.pillar_dir, f"{pillar_name}.yaml")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            config = yaml.safe_load(f)
        self._cache[pillar_name] = config
        return config

    def list_pillars(self) -> list[str]:
        pillars = []
        if not os.path.isdir(self.pillar_dir):
            return pillars
        for fname in os.listdir(self.pillar_dir):
            if fname.endswith(".yaml") or fname.endswith(".yml"):
                pillars.append(fname.rsplit(".", 1)[0])
        return sorted(pillars)

    def get_specialist_config(self, pillar_name: str, domain: str) -> dict | None:
        config = self.load(pillar_name)
        if config is None:
            return None
        return config.get("specialists", {}).get(domain)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_config/test_pillar_loader.py -v`
Expected: All 6 tests PASS

- [ ] **Step 6: Commit**

```bash
git add config/ tests/test_config/
git commit -m "feat: add pillar YAML configs and loader"
```

---

## Task 9: Domain Skills

**Files:**
- Create: `skills/domain/bureau.py`
- Create: `skills/domain/crossbu.py`
- Create: `skills/domain/modeling.py`
- Create: `skills/domain/spend_payments.py`
- Create: `skills/domain/wcc.py`
- Create: `skills/domain/customer_rel.py`
- Create: `skills/domain/capacity_afford.py`
- Create: `skills/domain/loader.py`
- Create: `tests/test_skills/__init__.py`
- Create: `tests/test_skills/test_domain_skills.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_skills/test_domain_skills.py
import pytest
from models.types import DomainSkill
from skills.domain.loader import load_domain_skill, list_domain_skills


def test_load_bureau_skill():
    skill = load_domain_skill("bureau")
    assert isinstance(skill, DomainSkill)
    assert skill.name == "bureau"
    assert "bureau" in skill.system_prompt.lower() or "tradeline" in skill.system_prompt.lower()
    assert len(skill.data_hints) > 0
    assert len(skill.risk_signals) > 0


def test_load_all_domain_skills():
    names = list_domain_skills()
    assert len(names) == 7
    for name in names:
        skill = load_domain_skill(name)
        assert isinstance(skill, DomainSkill)
        assert skill.name == name


def test_load_nonexistent_skill():
    skill = load_domain_skill("nonexistent")
    assert skill is None


def test_all_skills_have_required_fields():
    for name in list_domain_skills():
        skill = load_domain_skill(name)
        assert skill.system_prompt, f"{name} missing system_prompt"
        assert skill.data_hints, f"{name} missing data_hints"
        assert skill.interpretation_guide, f"{name} missing interpretation_guide"
        assert skill.risk_signals, f"{name} missing risk_signals"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_skills/test_domain_skills.py -v`
Expected: FAIL

- [ ] **Step 3: Implement domain skills**

Each domain skill is a function returning a `DomainSkill` instance. One file per domain.

```python
# skills/domain/bureau.py
from models.types import DomainSkill


def get_skill() -> DomainSkill:
    return DomainSkill(
        name="bureau",
        system_prompt=(
            "You are a bureau data specialist. You analyze credit bureau files "
            "including tradeline detail, derogatory marks, credit scores, hard inquiries, "
            "and utilization patterns. You interpret bureau data to assess credit health "
            "and delinquency risk."
        ),
        data_hints=["bureau_full", "bureau_trades"],
        interpretation_guide=(
            "Focus on tradeline health: number of active tradelines, derogatory marks, "
            "days-past-due status. A declining score with increasing derogs signals "
            "worsening credit. Pay attention to score_date — a stale score may not "
            "reflect recent payment behavior. High inquiry count may indicate credit seeking."
        ),
        risk_signals=[
            "90D+ delinquency on any tradeline",
            "Credit score below 600",
            "Score declining more than 50 points in 6 months",
            "Derogatory count increasing",
            "High utilization (>80%) across revolving tradelines",
        ],
    )
```

```python
# skills/domain/crossbu.py
from models.types import DomainSkill


def get_skill() -> DomainSkill:
    return DomainSkill(
        name="crossbu",
        system_prompt=(
            "You are a cross-business-unit exposure specialist. You analyze "
            "the customer's exposure across multiple Amex products — credit cards, "
            "charge cards, personal loans, savings. You assess concentration risk "
            "and cross-product contagion patterns."
        ),
        data_hints=["xbu_summary"],
        interpretation_guide=(
            "Look for concentrated exposure in a single product. High utilization "
            "across multiple products signals systemic stress. Compare exposure levels "
            "to income capacity. Watch for products where utilization exceeds 100%."
        ),
        risk_signals=[
            "Total cross-BU exposure exceeding estimated income",
            "Utilization above 100% on any product",
            "Concentrated exposure (>70% in single product)",
            "Multiple products showing simultaneous stress",
        ],
    )
```

```python
# skills/domain/modeling.py
from models.types import DomainSkill


def get_skill() -> DomainSkill:
    return DomainSkill(
        name="modeling",
        system_prompt=(
            "You are an internal model scoring specialist. You analyze outputs from "
            "risk models, propensity models, and collections models. You interpret "
            "score trajectories, percentile positions, and model signals in context."
        ),
        data_hints=["model_scores"],
        interpretation_guide=(
            "Compare scores across model types — a high risk_v3 score with low "
            "propensity_v2 may indicate different risk dimensions. Track score changes "
            "over time via timestamp. Percentile position relative to population is "
            "more informative than raw score."
        ),
        risk_signals=[
            "Risk score above 0.7 (high probability of default)",
            "Score worsening over consecutive scoring runs",
            "Percentile position in top decile for risk",
            "Divergence between risk model and propensity model",
        ],
    )
```

```python
# skills/domain/spend_payments.py
from models.types import DomainSkill


def get_skill() -> DomainSkill:
    return DomainSkill(
        name="spend_payments",
        system_prompt=(
            "You are a spend and payment behavior specialist. You analyze monthly "
            "transaction patterns, payment history, spending spikes, and delinquency "
            "trajectories. You assess whether payment behavior is stable, improving, "
            "or deteriorating."
        ),
        data_hints=["txn_monthly", "pmts_detail"],
        interpretation_guide=(
            "Track payment status over time — increasing late/missed payments signal "
            "deterioration. Compare spend levels to payment amounts — spending more "
            "than paying indicates growing balance. Look for spend spikes that may "
            "indicate distress spending or burst behavior."
        ),
        risk_signals=[
            "3+ missed payments in last 6 months",
            "Spend-to-payment ratio above 1.5",
            "Sudden spend spike (>2x average monthly spend)",
            "Accelerating late payment frequency",
            "Payment amounts declining while spend increases",
        ],
    )
```

```python
# skills/domain/wcc.py
from models.types import DomainSkill


def get_skill() -> DomainSkill:
    return DomainSkill(
        name="wcc",
        system_prompt=(
            "You are a watch-list and credit control specialist. You analyze WCC flags "
            "including overlimit alerts, rapid spend warnings, payment defaults, fraud "
            "alerts, and manual review triggers. You classify and prioritize control signals."
        ),
        data_hints=["wcc_flags"],
        interpretation_guide=(
            "Classify flags by severity and recency. Multiple flags clustering in time "
            "may indicate an acute event. High-severity flags (critical, high) demand "
            "immediate attention. Check whether flags align with other data — a fraud "
            "alert with no corresponding spend anomaly may be a false positive."
        ),
        risk_signals=[
            "Critical severity flag in last 30 days",
            "Multiple high-severity flags clustering",
            "Overlimit flag combined with payment default",
            "Rapid spend flag followed by missed payment",
        ],
    )
```

```python
# skills/domain/customer_rel.py
from models.types import DomainSkill


def get_skill() -> DomainSkill:
    return DomainSkill(
        name="customer_rel",
        system_prompt=(
            "You are a customer relationship specialist. You analyze tenure, product "
            "holdings, customer segment, and relationship depth. You assess the "
            "customer's value and loyalty indicators."
        ),
        data_hints=["cust_tenure"],
        interpretation_guide=(
            "Long tenure with multiple products signals a deep relationship — risk "
            "events on these customers have higher impact. Segment context matters: "
            "high-net-worth customers may have different risk tolerance. Short tenure "
            "with rapid product acquisition may indicate aggressive credit seeking."
        ),
        risk_signals=[
            "Short tenure (<12 months) with multiple products",
            "Recent product closures indicating relationship deterioration",
            "Segment mismatch (high segment but low-income indicators)",
        ],
    )
```

```python
# skills/domain/capacity_afford.py
from models.types import DomainSkill


def get_skill() -> DomainSkill:
    return DomainSkill(
        name="capacity_afford",
        system_prompt=(
            "You are a capacity and affordability specialist. You analyze income "
            "estimates, total debt obligations, and debt-to-income ratios to assess "
            "whether the customer has headroom for their current obligations."
        ),
        data_hints=["income_dti"],
        interpretation_guide=(
            "DTI ratio is the primary signal — above 0.43 is generally concerning. "
            "Compare total_debt to income_est for absolute capacity. Consider that "
            "income_est may be stale. A high DTI with rising debt signals a customer "
            "approaching capacity limits."
        ),
        risk_signals=[
            "DTI ratio above 0.43",
            "Total debt exceeding estimated annual income",
            "DTI ratio increasing over time",
            "Income estimate appears stale or unreliable",
        ],
    )
```

- [ ] **Step 4: Implement domain skill loader**

```python
# skills/domain/loader.py
from __future__ import annotations

import importlib
from models.types import DomainSkill

_DOMAIN_MODULES = {
    "bureau": "skills.domain.bureau",
    "crossbu": "skills.domain.crossbu",
    "modeling": "skills.domain.modeling",
    "spend_payments": "skills.domain.spend_payments",
    "wcc": "skills.domain.wcc",
    "customer_rel": "skills.domain.customer_rel",
    "capacity_afford": "skills.domain.capacity_afford",
}


def load_domain_skill(name: str) -> DomainSkill | None:
    module_path = _DOMAIN_MODULES.get(name)
    if module_path is None:
        return None
    module = importlib.import_module(module_path)
    return module.get_skill()


def list_domain_skills() -> list[str]:
    return sorted(_DOMAIN_MODULES.keys())
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_skills/test_domain_skills.py -v`
Expected: All 4 tests PASS

- [ ] **Step 6: Commit**

```bash
git add skills/ tests/test_skills/
git commit -m "feat: add 7 domain skills and skill loader"
```

---

## Task 10: Base Specialist Agent

**Files:**
- Create: `agents/base_agent.py`
- Create: `tests/test_agents/__init__.py`
- Create: `tests/test_agents/test_base_agent.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_agents/test_base_agent.py
import pytest
from unittest.mock import MagicMock, patch
from agents.base_agent import BaseSpecialistAgent
from models.types import DomainSkill, LLMResult, SpecialistOutput
from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger


@pytest.fixture
def domain_skill():
    return DomainSkill(
        name="bureau",
        system_prompt="You are a bureau expert.",
        data_hints=["bureau_full"],
        interpretation_guide="Focus on tradelines.",
        risk_signals=["90D+ delinquency"],
    )


@pytest.fixture
def pillar_yaml():
    return {"focus": "Delinquency Risk", "prompt_overlay": "Flag 90D+ Marks"}


@pytest.fixture
def mock_firewall(tmp_path):
    adapter = MagicMock()
    logger = EventLogger(session_id="test", log_dir=str(tmp_path))
    return FirewallStack(adapter=adapter, logger=logger)


@pytest.fixture
def agent(domain_skill, pillar_yaml, mock_firewall, tmp_path):
    logger = EventLogger(session_id="test", log_dir=str(tmp_path))
    return BaseSpecialistAgent(
        domain_skill=domain_skill,
        pillar_yaml=pillar_yaml,
        firewall=mock_firewall,
        logger=logger,
    )


def test_agent_creation(agent):
    assert agent.skill.name == "bureau"
    assert agent.rolling_summary == ""


def test_build_system_prompt(agent):
    prompt = agent._build_system_prompt()
    assert "bureau expert" in prompt.lower()
    assert "Delinquency Risk" in prompt
    assert "Flag 90D+ Marks" in prompt


def test_build_system_prompt_includes_rolling_summary(agent):
    agent.rolling_summary = "Previously found: 3 derog marks."
    prompt = agent._build_system_prompt()
    assert "Previously found: 3 derog marks." in prompt


def test_update_rolling_summary(agent):
    agent._update_rolling_summary("What is the score?", "Score is 720, no derog marks.")
    assert "What is the score?" in agent.rolling_summary
    assert "720" in agent.rolling_summary


def test_rolling_summary_truncation(agent):
    # Simulate many updates to trigger truncation
    for i in range(50):
        agent._update_rolling_summary(f"Question {i}?", f"Finding {i} with lots of detail " * 10)
    # Should not exceed budget (rough check — summary should be bounded)
    assert len(agent.rolling_summary) < 5000


def test_run_returns_specialist_output(agent, mock_firewall):
    """Test the full run() method with mocked LLM responses."""
    # Mock the firewall to return structured responses for each skill step
    mock_firewall.call = MagicMock(side_effect=[
        # data_request step
        LLMResult(status="success", data={
            "intent": "delinquency history",
            "variables": ["derog_count", "score"],
            "table_hints": ["bureau_full"],
        }),
        # synthesize step
        LLMResult(status="success", data={
            "findings": "3 derog marks, score 620",
            "evidence": ["bureau_full.derog_count = 3"],
            "implications": ["Elevated delinquency risk"],
            "data_gaps": [],
        }),
        # answer step
        LLMResult(status="success", data={
            "answer": "The delinquency trajectory shows worsening risk.",
            "evidence": ["3 derog marks in 12 months"],
        }),
    ])

    output = agent.run("What is the delinquency trajectory?", mode="chat")
    assert isinstance(output, SpecialistOutput)
    assert output.domain == "bureau"
    assert output.question == "What is the delinquency trajectory?"
    assert output.mode == "chat"
    assert mock_firewall.call.call_count == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agents/test_base_agent.py -v`
Expected: FAIL

- [ ] **Step 3: Implement BaseSpecialistAgent**

```python
# agents/base_agent.py
from __future__ import annotations

import json

from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger
from models.types import DomainSkill, LLMResult, SpecialistOutput
from tools.data_tools import list_available_tables, get_table_schema, query_table

ROLLING_SUMMARY_MAX_CHARS = 3000

BASE_INSTRUCTIONS = """You are a domain specialist in a case review system. You analyze data and produce evidence-backed findings.

You have access to these tools:
- list_available_tables(): List all data tables
- get_table_schema(table_name): Get column details for a table
- query_table(table_name, filter_column, filter_value, limit): Query data

Always ground your findings in data. If data is unavailable, explicitly state what is missing and whether the absence may itself be a signal.

Respond in JSON format as instructed in each step."""


class BaseSpecialistAgent:
    def __init__(self, domain_skill: DomainSkill, pillar_yaml: dict,
                 firewall: FirewallStack, logger: EventLogger):
        self.skill = domain_skill
        self.pillar = pillar_yaml
        self.firewall = firewall
        self.logger = logger
        self.rolling_summary = ""
        self._questions_answered: list[str] = []

    @property
    def questions_answered(self) -> int:
        return len(self._questions_answered)

    def _build_system_prompt(self) -> str:
        parts = [
            BASE_INSTRUCTIONS,
            "",
            "--- DOMAIN EXPERTISE ---",
            self.skill.system_prompt,
            "",
            f"Data hints: {', '.join(self.skill.data_hints)}",
            f"Interpretation guide: {self.skill.interpretation_guide}",
            f"Risk signals to watch: {', '.join(self.skill.risk_signals)}",
            "",
            "--- PILLAR CONTEXT ---",
            f"Focus: {self.pillar.get('focus', '')}",
            f"Overlay: {self.pillar.get('prompt_overlay', '')}",
        ]
        if self.rolling_summary:
            parts.extend([
                "",
                "--- PRIOR FINDINGS IN THIS SESSION ---",
                self.rolling_summary,
            ])
        return "\n".join(parts)

    def _update_rolling_summary(self, question: str, findings: str) -> None:
        entry = f"Q: {question}\nA: {findings}\n---\n"
        self.rolling_summary += entry
        self._questions_answered.append(question)
        # Truncate if too long — keep most recent entries
        if len(self.rolling_summary) > ROLLING_SUMMARY_MAX_CHARS:
            entries = self.rolling_summary.split("---\n")
            while len("\n---\n".join(entries)) > ROLLING_SUMMARY_MAX_CHARS and len(entries) > 1:
                entries.pop(0)
            self.rolling_summary = "---\n".join(entries)

    def run(self, question: str, mode: str) -> SpecialistOutput:
        system_prompt = self._build_system_prompt()
        tools = [list_available_tables, get_table_schema, query_table]

        # Step 1: Data Request
        data_request_msg = (
            f"STEP: DATA_REQUEST\n"
            f"Question to answer: {question}\n"
            f"Determine what data you need. Respond with JSON:\n"
            f'{{"intent": "...", "variables": [...], "table_hints": [...]}}'
        )
        self.logger.log("data_request", {"domain": self.skill.name, "question": question})
        dr_result = self.firewall.call(system_prompt, data_request_msg, tools=tools)

        if dr_result.status == "blocked":
            self.logger.log("firewall_blocked", {"domain": self.skill.name, "step": "data_request"})
            return self._blocked_output(question, mode, "data_request", dr_result.error or "")

        self.logger.log("data_response", {
            "domain": self.skill.name, "result": dr_result.data,
        })

        # Step 2: Synthesize
        synth_msg = (
            f"STEP: SYNTHESIZE\n"
            f"Question: {question}\n"
            f"Data retrieved: {json.dumps(dr_result.data, default=str)}\n"
            f"Synthesize findings. Respond with JSON:\n"
            f'{{"findings": "...", "evidence": [...], "implications": [...], "data_gaps": [...]}}'
        )
        synth_result = self.firewall.call(system_prompt, synth_msg, tools=tools)

        if synth_result.status == "blocked":
            self.logger.log("firewall_blocked", {"domain": self.skill.name, "step": "synthesize"})
            return self._blocked_output(question, mode, "synthesize", synth_result.error or "")

        self.logger.log("synthesis", {
            "domain": self.skill.name, "result": synth_result.data,
        })

        # Step 3: Report or Answer
        if mode == "report":
            output_msg = (
                f"STEP: REPORT\n"
                f"Question: {question}\n"
                f"Findings: {json.dumps(synth_result.data, default=str)}\n"
                f"Produce a report section. Respond with JSON:\n"
                f'{{"key_findings": "...", "supporting_evidence": [...], "risk_implication": "..."}}'
            )
            event_type = "report_generated"
        else:
            output_msg = (
                f"STEP: ANSWER\n"
                f"Question: {question}\n"
                f"Findings: {json.dumps(synth_result.data, default=str)}\n"
                f"Answer the question directly. Respond with JSON:\n"
                f'{{"answer": "...", "evidence": [...]}}'
            )
            event_type = "answer_generated"

        out_result = self.firewall.call(system_prompt, output_msg, tools=tools)

        if out_result.status == "blocked":
            self.logger.log("firewall_blocked", {"domain": self.skill.name, "step": mode})
            return self._blocked_output(question, mode, mode, out_result.error or "")

        self.logger.log(event_type, {"domain": self.skill.name, "result": out_result.data})

        # Build output
        synth_data = synth_result.data or {}
        out_data = out_result.data or {}
        findings = synth_data.get("findings", out_data.get("answer", ""))
        evidence = synth_data.get("evidence", []) + out_data.get("evidence", [])
        implications = synth_data.get("implications", [])
        data_gaps = synth_data.get("data_gaps", [])

        self._update_rolling_summary(question, findings)

        return SpecialistOutput(
            domain=self.skill.name,
            question=question,
            mode=mode,
            findings=findings,
            evidence=evidence,
            implications=implications,
            data_gaps=data_gaps,
            raw_data=dr_result.data or {},
        )

    def _blocked_output(self, question: str, mode: str, step: str, error: str) -> SpecialistOutput:
        return SpecialistOutput(
            domain=self.skill.name,
            question=question,
            mode=mode,
            findings=f"Analysis incomplete — blocked at {step} step.",
            evidence=[],
            implications=[],
            data_gaps=[f"Firewall blocked {step}: {error}"],
            raw_data={},
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_agents/test_base_agent.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add agents/base_agent.py tests/test_agents/
git commit -m "feat: add BaseSpecialistAgent with 3-step skill chain"
```

---

## Task 11: Session Registry

**Files:**
- Create: `agents/session_registry.py`
- Create: `tests/test_agents/test_session_registry.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_agents/test_session_registry.py
import pytest
from unittest.mock import MagicMock
from agents.session_registry import SessionRegistry
from agents.base_agent import BaseSpecialistAgent
from models.types import DomainSkill
from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test", log_dir=str(tmp_path))


@pytest.fixture
def mock_firewall(logger):
    adapter = MagicMock()
    return FirewallStack(adapter=adapter, logger=logger)


@pytest.fixture
def bureau_skill():
    return DomainSkill(
        name="bureau", system_prompt="Bureau expert.",
        data_hints=["bureau_full"], interpretation_guide="Tradelines.",
        risk_signals=["90D+"],
    )


@pytest.fixture
def registry():
    return SessionRegistry()


def test_create_new_specialist(registry, bureau_skill, mock_firewall, logger):
    pillar = {"focus": "Delinquency Risk", "prompt_overlay": "Flag 90D+"}
    agent = registry.get_or_create("bureau", "credit_risk", bureau_skill, pillar, mock_firewall, logger)
    assert isinstance(agent, BaseSpecialistAgent)
    assert agent.skill.name == "bureau"


def test_reuse_existing_specialist(registry, bureau_skill, mock_firewall, logger):
    pillar = {"focus": "Delinquency Risk", "prompt_overlay": "Flag 90D+"}
    agent1 = registry.get_or_create("bureau", "credit_risk", bureau_skill, pillar, mock_firewall, logger)
    agent1.rolling_summary = "Prior findings here."
    agent2 = registry.get_or_create("bureau", "credit_risk", bureau_skill, pillar, mock_firewall, logger)
    assert agent1 is agent2
    assert agent2.rolling_summary == "Prior findings here."


def test_different_pillar_creates_new(registry, bureau_skill, mock_firewall, logger):
    pillar_cr = {"focus": "Delinquency Risk", "prompt_overlay": "Flag 90D+"}
    pillar_cbo = {"focus": "Limit Utilisation", "prompt_overlay": "Burst Risk"}
    agent_cr = registry.get_or_create("bureau", "credit_risk", bureau_skill, pillar_cr, mock_firewall, logger)
    agent_cbo = registry.get_or_create("bureau", "cbo", bureau_skill, pillar_cbo, mock_firewall, logger)
    assert agent_cr is not agent_cbo


def test_list_active(registry, bureau_skill, mock_firewall, logger):
    pillar = {"focus": "Delinquency Risk", "prompt_overlay": "Flag 90D+"}
    registry.get_or_create("bureau", "credit_risk", bureau_skill, pillar, mock_firewall, logger)
    active = registry.list_active()
    assert len(active) == 1
    assert active[0]["domain"] == "bureau"
    assert active[0]["pillar"] == "credit_risk"


def test_clear(registry, bureau_skill, mock_firewall, logger):
    pillar = {"focus": "Delinquency Risk", "prompt_overlay": "Flag 90D+"}
    registry.get_or_create("bureau", "credit_risk", bureau_skill, pillar, mock_firewall, logger)
    registry.clear()
    assert len(registry.list_active()) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agents/test_session_registry.py -v`
Expected: FAIL

- [ ] **Step 3: Implement SessionRegistry**

```python
# agents/session_registry.py
from __future__ import annotations

from agents.base_agent import BaseSpecialistAgent
from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger
from models.types import DomainSkill


class SessionRegistry:
    def __init__(self):
        self._active: dict[tuple[str, str], BaseSpecialistAgent] = {}

    def get_or_create(self, domain: str, pillar: str,
                      domain_skill: DomainSkill, pillar_yaml: dict,
                      firewall: FirewallStack, logger: EventLogger) -> BaseSpecialistAgent:
        key = (domain, pillar)
        if key in self._active:
            logger.log("specialist_reused", {
                "domain": domain, "pillar": pillar,
                "prior_questions": self._active[key].questions_answered,
            })
            return self._active[key]

        agent = BaseSpecialistAgent(
            domain_skill=domain_skill,
            pillar_yaml=pillar_yaml,
            firewall=firewall,
            logger=logger,
        )
        self._active[key] = agent
        logger.log("specialist_invoked", {
            "domain": domain, "pillar": pillar, "reused": False,
        })
        return agent

    def list_active(self) -> list[dict]:
        return [
            {
                "domain": domain,
                "pillar": pillar,
                "questions_answered": agent.questions_answered,
                "summary_preview": agent.rolling_summary[:200],
            }
            for (domain, pillar), agent in self._active.items()
        ]

    def clear(self) -> None:
        self._active.clear()
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_agents/test_session_registry.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add agents/session_registry.py tests/test_agents/test_session_registry.py
git commit -m "feat: add session registry for specialist reuse"
```

---

## Task 12: General Specialist & Compare Skill

**Files:**
- Create: `agents/general_specialist.py`
- Create: `skills/compare.py`
- Create: `tests/test_agents/test_general_specialist.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_agents/test_general_specialist.py
import pytest
from unittest.mock import MagicMock
from agents.general_specialist import GeneralSpecialist
from models.types import SpecialistOutput, ReviewReport, LLMResult
from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test", log_dir=str(tmp_path))


@pytest.fixture
def mock_firewall(logger):
    adapter = MagicMock()
    return FirewallStack(adapter=adapter, logger=logger)


@pytest.fixture
def specialist_outputs():
    return {
        "bureau": SpecialistOutput(
            domain="bureau", question="Risk assessment?", mode="chat",
            findings="Low delinquency risk — score 720, no 90D+ marks",
            evidence=["score=720", "derog_count=0"],
            implications=["Low delinquency risk"],
            data_gaps=[], raw_data={},
        ),
        "spend_payments": SpecialistOutput(
            domain="spend_payments", question="Risk assessment?", mode="chat",
            findings="Accelerating late payments — 3 missed in last 4 months",
            evidence=["3 missed payments since Sep 2024"],
            implications=["Payment behavior deteriorating rapidly"],
            data_gaps=[], raw_data={},
        ),
    }


def test_general_specialist_creation(mock_firewall, logger):
    gs = GeneralSpecialist(firewall=mock_firewall, logger=logger)
    assert gs is not None


def test_generate_pairs(mock_firewall, logger, specialist_outputs):
    gs = GeneralSpecialist(firewall=mock_firewall, logger=logger)
    pairs = gs._generate_pairs(list(specialist_outputs.keys()))
    assert ("bureau", "spend_payments") in pairs


def test_generate_pairs_three_specialists(mock_firewall, logger):
    gs = GeneralSpecialist(firewall=mock_firewall, logger=logger)
    pairs = gs._generate_pairs(["bureau", "modeling", "spend_payments"])
    assert len(pairs) == 3  # C(3,2) = 3


def test_compare_returns_review_report(mock_firewall, logger, specialist_outputs):
    gs = GeneralSpecialist(firewall=mock_firewall, logger=logger)

    # Mock firewall to return compare analysis
    mock_firewall.call = MagicMock(return_value=LLMResult(
        status="success",
        data={
            "contradictions": [{
                "pair": ["bureau", "spend_payments"],
                "contradiction": "Bureau says low risk but payments deteriorating",
                "question": "Is the bureau score lagging?",
                "answer": "Likely — score may not reflect recent missed payments",
                "supporting_evidence": ["score_date unknown", "3 missed payments recent"],
                "conclusion": "Payment behavior is the more current signal",
                "resolved": True,
            }],
            "cross_domain_insights": ["Bureau-payment divergence suggests score staleness"],
        },
    ))

    report = gs.compare(specialist_outputs, "Risk assessment?")
    assert isinstance(report, ReviewReport)
    assert len(report.resolved) == 1
    assert report.resolved[0].pair == ("bureau", "spend_payments")


def test_compare_single_specialist(mock_firewall, logger):
    """With only one specialist, no pairs — return empty report."""
    gs = GeneralSpecialist(firewall=mock_firewall, logger=logger)
    outputs = {
        "bureau": SpecialistOutput(
            domain="bureau", question="test", mode="chat",
            findings="test", evidence=[], implications=[],
            data_gaps=[], raw_data={},
        ),
    }
    report = gs.compare(outputs, "test")
    assert isinstance(report, ReviewReport)
    assert len(report.resolved) == 0
    assert len(report.open_conflicts) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agents/test_general_specialist.py -v`
Expected: FAIL

- [ ] **Step 3: Implement GeneralSpecialist**

```python
# agents/general_specialist.py
from __future__ import annotations

import json
from itertools import combinations

from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger
from models.types import (
    Conflict, Resolution, ReviewReport, SpecialistOutput, LLMResult,
)
from tools.data_tools import list_available_tables, get_table_schema, query_table

COMPARE_SYSTEM_PROMPT = """You are a General Specialist — a cross-domain reviewer. You do NOT have domain expertise. Your job is to:

1. Read the outputs from multiple domain specialists
2. Compare their implications pairwise
3. Identify contradictions — where two specialists' conclusions conflict
4. For each contradiction: formulate a precise question, then answer it yourself by reasoning across the combined evidence
5. If existing evidence is insufficient, request additional data
6. If a contradiction cannot be resolved, flag it as an open conflict

You have access to data tools for additional queries:
- list_available_tables(): List all data tables
- get_table_schema(table_name): Get column details
- query_table(table_name, filter_column, filter_value, limit): Query data

Respond in JSON format as instructed."""


class GeneralSpecialist:
    def __init__(self, firewall: FirewallStack, logger: EventLogger):
        self.firewall = firewall
        self.logger = logger

    def compare(self, specialist_outputs: dict[str, SpecialistOutput],
                question: str) -> ReviewReport:
        domains = list(specialist_outputs.keys())

        if len(domains) < 2:
            return ReviewReport()

        pairs = self._generate_pairs(domains)
        self.logger.log("compare_start", {
            "pairs": [list(p) for p in pairs],
            "question": question,
        })

        # Build context from all specialist outputs
        outputs_context = self._format_outputs_for_prompt(specialist_outputs)

        # Ask LLM to analyze all pairs at once
        tools = [list_available_tables, get_table_schema, query_table]
        compare_msg = (
            f"TASK: PAIRWISE COMPARE\n"
            f"Original question: {question}\n\n"
            f"Specialist outputs:\n{outputs_context}\n\n"
            f"Pairs to compare: {[list(p) for p in pairs]}\n\n"
            f"For each pair, check if their implications contradict.\n"
            f"For each contradiction found:\n"
            f"  1. Describe the contradiction\n"
            f"  2. Formulate a precise question about it\n"
            f"  3. Answer the question using combined evidence (query data if needed)\n"
            f"  4. State your conclusion and whether it is resolved\n\n"
            f"Also note any cross-domain insights.\n\n"
            f"Respond with JSON:\n"
            f'{{"contradictions": [{{"pair": ["a","b"], "contradiction": "...", '
            f'"question": "...", "answer": "...", "supporting_evidence": [...], '
            f'"conclusion": "...", "resolved": true/false}}], '
            f'"cross_domain_insights": [...]}}'
        )

        result = self.firewall.call(COMPARE_SYSTEM_PROMPT, compare_msg, tools=tools)

        if result.status == "blocked":
            self.logger.log("firewall_blocked", {"step": "compare"})
            return ReviewReport()

        return self._parse_compare_result(result)

    def _generate_pairs(self, domains: list[str]) -> list[tuple[str, str]]:
        return list(combinations(sorted(domains), 2))

    def _format_outputs_for_prompt(self, outputs: dict[str, SpecialistOutput]) -> str:
        parts = []
        for domain, output in outputs.items():
            parts.append(
                f"[{domain.upper()}]\n"
                f"Findings: {output.findings}\n"
                f"Evidence: {', '.join(output.evidence)}\n"
                f"Implications: {', '.join(output.implications)}\n"
                f"Data gaps: {', '.join(output.data_gaps) if output.data_gaps else 'None'}\n"
            )
        return "\n".join(parts)

    def _parse_compare_result(self, result: LLMResult) -> ReviewReport:
        data = result.data or {}
        resolved = []
        open_conflicts = []
        insights = data.get("cross_domain_insights", [])

        for c in data.get("contradictions", []):
            pair_raw = c.get("pair", [])
            pair = (pair_raw[0], pair_raw[1]) if len(pair_raw) >= 2 else ("unknown", "unknown")

            if c.get("resolved", False):
                self.logger.log("contradiction_found", {
                    "pair": list(pair), "contradiction": c.get("contradiction", ""),
                })
                self.logger.log("question_raised", {"question": c.get("question", "")})
                self.logger.log("self_answer", {
                    "resolution": "resolved", "conclusion": c.get("conclusion", ""),
                })
                resolved.append(Resolution(
                    pair=pair,
                    contradiction=c.get("contradiction", ""),
                    question_raised=c.get("question", ""),
                    answer=c.get("answer", ""),
                    supporting_evidence=c.get("supporting_evidence", []),
                    conclusion=c.get("conclusion", ""),
                ))
            else:
                self.logger.log("contradiction_found", {
                    "pair": list(pair), "contradiction": c.get("contradiction", ""),
                })
                self.logger.log("self_answer", {
                    "resolution": "open_conflict", "reason": c.get("conclusion", ""),
                })
                open_conflicts.append(Conflict(
                    pair=pair,
                    contradiction=c.get("contradiction", ""),
                    question_raised=c.get("question", ""),
                    reason_unresolved=c.get("conclusion", ""),
                    evidence_from_both=c.get("supporting_evidence", []),
                ))

        return ReviewReport(
            resolved=resolved,
            open_conflicts=open_conflicts,
            cross_domain_insights=insights,
            data_requests_made=[],
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_agents/test_general_specialist.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add agents/general_specialist.py skills/compare.py tests/test_agents/test_general_specialist.py
git commit -m "feat: add General Specialist with Compare skill"
```

---

## Task 13: Orchestrator — Team Construction & Synthesize

**Files:**
- Create: `orchestrator/orchestrator.py`
- Create: `orchestrator/team.py`
- Create: `tests/test_orchestrator/__init__.py`
- Create: `tests/test_orchestrator/test_orchestrator.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_orchestrator/test_orchestrator.py
import pytest
from unittest.mock import MagicMock, patch
from orchestrator.orchestrator import Orchestrator
from orchestrator.team import TeamConstructor
from agents.session_registry import SessionRegistry
from models.types import (
    SpecialistOutput, ReviewReport, Resolution, Conflict,
    DataGap, FinalOutput, LLMResult,
)
from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test", log_dir=str(tmp_path))


@pytest.fixture
def mock_firewall(logger):
    adapter = MagicMock()
    return FirewallStack(adapter=adapter, logger=logger)


# --- TeamConstructor tests ---

def test_team_constructor_selects_specialists(mock_firewall, logger):
    tc = TeamConstructor(firewall=mock_firewall, logger=logger)
    mock_firewall.call = MagicMock(return_value=LLMResult(
        status="success",
        data={"specialists": ["bureau", "modeling"]},
    ))
    selected = tc.select_specialists(
        question="What is the delinquency risk?",
        pillar="credit_risk",
        available_specialists=["bureau", "crossbu", "modeling", "spend_payments"],
        active_specialists=[],
    )
    assert "bureau" in selected
    assert "modeling" in selected


# --- Orchestrator synthesize tests ---

def test_synthesize_merges_outputs(mock_firewall, logger):
    orch = Orchestrator(
        firewall=mock_firewall,
        logger=logger,
        registry=SessionRegistry(),
        pillar="credit_risk",
    )

    specialist_outputs = {
        "bureau": SpecialistOutput(
            domain="bureau", question="Risk?", mode="chat",
            findings="Score 720, no derog", evidence=["score=720"],
            implications=["Low risk"], data_gaps=[], raw_data={},
        ),
    }
    review_report = ReviewReport()

    mock_firewall.call = MagicMock(return_value=LLMResult(
        status="success",
        data={
            "answer": "Based on bureau data, risk is low.",
            "data_gap_assessments": [],
        },
    ))

    final = orch.synthesize(specialist_outputs, review_report, "Risk?", "chat")
    assert isinstance(final, FinalOutput)
    assert "bureau" in final.specialists_consulted


def test_synthesize_includes_open_conflicts(mock_firewall, logger):
    orch = Orchestrator(
        firewall=mock_firewall,
        logger=logger,
        registry=SessionRegistry(),
        pillar="credit_risk",
    )

    specialist_outputs = {
        "bureau": SpecialistOutput(
            domain="bureau", question="Risk?", mode="chat",
            findings="Low risk", evidence=[], implications=["Low risk"],
            data_gaps=[], raw_data={},
        ),
        "spend_payments": SpecialistOutput(
            domain="spend_payments", question="Risk?", mode="chat",
            findings="Deteriorating", evidence=[], implications=["High risk"],
            data_gaps=[], raw_data={},
        ),
    }
    review_report = ReviewReport(
        open_conflicts=[Conflict(
            pair=("bureau", "spend_payments"),
            contradiction="Low vs High risk",
            question_raised="Which signal is current?",
            reason_unresolved="Need bureau score date",
            evidence_from_both=[],
        )],
    )

    mock_firewall.call = MagicMock(return_value=LLMResult(
        status="success",
        data={
            "answer": "Conflicting signals between bureau and payments.",
            "data_gap_assessments": [],
        },
    ))

    final = orch.synthesize(specialist_outputs, review_report, "Risk?", "chat")
    assert len(final.open_conflicts) == 1


def test_synthesize_handles_data_gaps(mock_firewall, logger):
    orch = Orchestrator(
        firewall=mock_firewall,
        logger=logger,
        registry=SessionRegistry(),
        pillar="credit_risk",
    )

    specialist_outputs = {
        "modeling": SpecialistOutput(
            domain="modeling", question="Risk?", mode="chat",
            findings="No model scores available",
            evidence=[], implications=[],
            data_gaps=["model_scores table empty"], raw_data={},
        ),
    }
    review_report = ReviewReport()

    mock_firewall.call = MagicMock(return_value=LLMResult(
        status="success",
        data={
            "answer": "Model scoring data unavailable.",
            "data_gap_assessments": [{
                "specialist": "modeling",
                "missing_data": "model_scores table empty",
                "absence_interpretation": "Customer may be below scoring threshold",
                "is_signal": True,
            }],
        },
    ))

    final = orch.synthesize(specialist_outputs, review_report, "Risk?", "chat")
    assert len(final.data_gaps) == 1
    assert final.data_gaps[0].is_signal is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_orchestrator/test_orchestrator.py -v`
Expected: FAIL

- [ ] **Step 3: Implement TeamConstructor**

```python
# orchestrator/team.py
from __future__ import annotations

from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger
from models.types import LLMResult

TEAM_CONSTRUCTION_PROMPT = """You are a team construction specialist. Given a question, a pillar context, and a list of available domain specialists, select which specialists are needed to answer the question.

Rules:
- Select only specialists whose domain is relevant to the question
- Prefer reusing active specialists (they have prior context)
- Do not select all specialists by default — be targeted
- If the question is narrow, 1-2 specialists may suffice
- If the question is broad or cross-domain, select more

Respond with JSON: {"specialists": ["bureau", "modeling", ...]}"""


class TeamConstructor:
    def __init__(self, firewall: FirewallStack, logger: EventLogger):
        self.firewall = firewall
        self.logger = logger

    def select_specialists(self, question: str, pillar: str,
                           available_specialists: list[str],
                           active_specialists: list[dict]) -> list[str]:
        active_info = ""
        if active_specialists:
            active_info = "\nCurrently active specialists (warm, have prior context):\n"
            for a in active_specialists:
                active_info += f"  - {a['domain']} ({a['questions_answered']} questions answered)\n"

        msg = (
            f"Question: {question}\n"
            f"Pillar: {pillar}\n"
            f"Available specialists: {', '.join(available_specialists)}\n"
            f"{active_info}\n"
            f"Select the specialists needed. Respond with JSON:\n"
            f'{{"specialists": [...]}}'
        )

        result = self.firewall.call(TEAM_CONSTRUCTION_PROMPT, msg)

        if result.status == "blocked":
            self.logger.log("firewall_blocked", {"step": "team_construction"})
            return available_specialists  # fallback: use all

        data = result.data or {}
        selected = data.get("specialists", available_specialists)
        # Validate — only return names that are actually available
        valid = [s for s in selected if s in available_specialists]
        return valid if valid else available_specialists
```

- [ ] **Step 4: Implement Orchestrator**

```python
# orchestrator/orchestrator.py
from __future__ import annotations

import json

from agents.session_registry import SessionRegistry
from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger
from models.types import (
    Conflict, DataGap, BlockedStep, FinalOutput, LLMResult,
    ReviewReport, SpecialistOutput,
)

SYNTHESIZE_PROMPT = """You are the orchestrator synthesizer. You merge specialist outputs and a cross-specialist review report into one coherent final answer.

Rules:
1. Weave specialist findings into a coherent narrative
2. For resolved contradictions: use the General Specialist's resolution, not the raw conflicting outputs
3. For open conflicts: present both sides clearly and flag them for human judgment
4. For data gaps: evaluate whether the ABSENCE of data is itself a signal. Missing data does NOT mean "nothing to report" — it may mean the customer lacks credit history, was never scored, etc. State your assessment explicitly.
5. Never silently omit a blocked or missing analysis — surface what is incomplete and why
6. Ground all claims in evidence

Respond with JSON:
{"answer": "...", "data_gap_assessments": [{"specialist": "...", "missing_data": "...", "absence_interpretation": "...", "is_signal": true/false}]}"""


class Orchestrator:
    def __init__(self, firewall: FirewallStack, logger: EventLogger,
                 registry: SessionRegistry, pillar: str):
        self.firewall = firewall
        self.logger = logger
        self.registry = registry
        self.pillar = pillar

    def synthesize(self, specialist_outputs: dict[str, SpecialistOutput],
                   review_report: ReviewReport,
                   question: str, mode: str) -> FinalOutput:
        self.logger.log("orchestrator_synthesize", {
            "question": question,
            "specialists": list(specialist_outputs.keys()),
            "resolved_contradictions": len(review_report.resolved),
            "open_conflicts": len(review_report.open_conflicts),
        })

        # Build context for synthesis
        context = self._build_synthesis_context(specialist_outputs, review_report, question)

        result = self.firewall.call(SYNTHESIZE_PROMPT, context)

        if result.status == "blocked":
            self.logger.log("firewall_blocked", {"step": "orchestrator_synthesize"})
            return FinalOutput(
                answer="Synthesis blocked by firewall. Specialist outputs are available individually.",
                resolved_contradictions=review_report.resolved,
                open_conflicts=review_report.open_conflicts,
                specialists_consulted=list(specialist_outputs.keys()),
            )

        data = result.data or {}
        answer = data.get("answer", "")

        # Parse data gap assessments from LLM
        data_gaps = []
        for gap in data.get("data_gap_assessments", []):
            data_gaps.append(DataGap(
                specialist=gap.get("specialist", ""),
                missing_data=gap.get("missing_data", ""),
                absence_interpretation=gap.get("absence_interpretation", ""),
                is_signal=gap.get("is_signal", False),
            ))

        # Also include data gaps from specialist outputs not covered by LLM
        for domain, output in specialist_outputs.items():
            for gap_desc in output.data_gaps:
                if not any(g.specialist == domain and g.missing_data == gap_desc for g in data_gaps):
                    self.logger.log("data_gap_flagged", {
                        "specialist": domain, "missing_data": gap_desc,
                    })

        # Collect blocked steps
        blocked_steps = []
        for domain, output in specialist_outputs.items():
            if "blocked" in output.findings.lower() and "incomplete" in output.findings.lower():
                blocked_steps.append(BlockedStep(
                    specialist=domain,
                    step="unknown",
                    error=output.findings,
                    attempts=0,
                ))

        final = FinalOutput(
            answer=answer,
            resolved_contradictions=review_report.resolved,
            open_conflicts=review_report.open_conflicts,
            data_gaps=data_gaps,
            blocked_steps=blocked_steps,
            specialists_consulted=list(specialist_outputs.keys()),
        )

        self.logger.log("final_output", {
            "open_conflicts": len(final.open_conflicts),
            "data_gaps": len(final.data_gaps),
            "blocked_steps": len(final.blocked_steps),
        })

        return final

    def _build_synthesis_context(self, specialist_outputs: dict[str, SpecialistOutput],
                                  review_report: ReviewReport, question: str) -> str:
        parts = [f"Original question: {question}\n"]

        parts.append("=== SPECIALIST OUTPUTS ===")
        for domain, output in specialist_outputs.items():
            parts.append(
                f"\n[{domain.upper()}]\n"
                f"Findings: {output.findings}\n"
                f"Evidence: {', '.join(output.evidence)}\n"
                f"Implications: {', '.join(output.implications)}\n"
                f"Data gaps: {', '.join(output.data_gaps) if output.data_gaps else 'None'}"
            )

        if review_report.resolved:
            parts.append("\n=== RESOLVED CONTRADICTIONS (from General Specialist) ===")
            for r in review_report.resolved:
                parts.append(
                    f"\nPair: {r.pair[0]} vs {r.pair[1]}\n"
                    f"Contradiction: {r.contradiction}\n"
                    f"Question: {r.question_raised}\n"
                    f"Resolution: {r.answer}\n"
                    f"Conclusion: {r.conclusion}"
                )

        if review_report.open_conflicts:
            parts.append("\n=== OPEN CONFLICTS (unresolved — flag for human) ===")
            for c in review_report.open_conflicts:
                parts.append(
                    f"\nPair: {c.pair[0]} vs {c.pair[1]}\n"
                    f"Contradiction: {c.contradiction}\n"
                    f"Why unresolved: {c.reason_unresolved}"
                )

        if review_report.cross_domain_insights:
            parts.append("\n=== CROSS-DOMAIN INSIGHTS ===")
            for insight in review_report.cross_domain_insights:
                parts.append(f"- {insight}")

        parts.append(
            "\n\nSynthesize all of the above into a coherent answer. "
            "For any data gaps, assess whether the absence is a signal."
        )

        return "\n".join(parts)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_orchestrator/test_orchestrator.py -v`
Expected: All 4 tests PASS

- [ ] **Step 6: Commit**

```bash
git add orchestrator/ tests/test_orchestrator/
git commit -m "feat: add orchestrator with team construction and synthesis"
```

---

## Task 14: Chat Agent

**Files:**
- Create: `orchestrator/chat_agent.py`
- Create: `tests/test_orchestrator/test_chat_agent.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_orchestrator/test_chat_agent.py
import pytest
from unittest.mock import MagicMock
from orchestrator.chat_agent import ChatAgent
from models.types import FinalOutput, LLMResult
from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test", log_dir=str(tmp_path))


@pytest.fixture
def mock_firewall(logger):
    adapter = MagicMock()
    return FirewallStack(adapter=adapter, logger=logger)


@pytest.fixture
def chat_agent(mock_firewall, logger):
    return ChatAgent(firewall=mock_firewall, logger=logger)


def test_format_for_reviewer(chat_agent):
    final = FinalOutput(
        answer="Risk is moderate based on bureau and payment data.",
        resolved_contradictions=[],
        open_conflicts=[],
        data_gaps=[],
        blocked_steps=[],
        specialists_consulted=["bureau", "spend_payments"],
    )
    formatted = chat_agent.format_for_reviewer(final)
    assert "Risk is moderate" in formatted
    assert "bureau" in formatted.lower()


def test_converse_returns_response(chat_agent, mock_firewall):
    mock_firewall.call = MagicMock(return_value=LLMResult(
        status="success",
        data={"raw": "I can help you understand that finding."},
    ))
    response = chat_agent.converse("Can you explain the bureau finding?", context="Prior answer: ...")
    assert isinstance(response, str)
    assert len(response) > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_orchestrator/test_chat_agent.py -v`
Expected: FAIL

- [ ] **Step 3: Implement ChatAgent**

```python
# orchestrator/chat_agent.py
from __future__ import annotations

from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger
from models.types import FinalOutput

CHAT_SYSTEM_PROMPT = """You are a case review assistant. You help reviewers understand the analysis produced by the specialist team. You can:
- Explain findings in plain language
- Clarify what evidence supports a conclusion
- Highlight areas flagged for human judgment
- Answer follow-up questions based on the analysis context

Be concise and evidence-grounded. Do not speculate beyond what the data shows."""


class ChatAgent:
    def __init__(self, firewall: FirewallStack, logger: EventLogger):
        self.firewall = firewall
        self.logger = logger
        self._conversation_history: list[dict] = []

    def format_for_reviewer(self, final_output: FinalOutput) -> str:
        parts = [final_output.answer]

        if final_output.open_conflicts:
            parts.append("\n--- REQUIRES YOUR ATTENTION ---")
            for c in final_output.open_conflicts:
                parts.append(
                    f"Unresolved conflict between {c.pair[0]} and {c.pair[1]}:\n"
                    f"  {c.contradiction}\n"
                    f"  Reason: {c.reason_unresolved}"
                )

        if final_output.data_gaps:
            parts.append("\n--- DATA GAPS ---")
            for g in final_output.data_gaps:
                signal_note = " (this absence may be a signal)" if g.is_signal else ""
                parts.append(
                    f"  {g.specialist}: {g.missing_data}{signal_note}\n"
                    f"  Interpretation: {g.absence_interpretation}"
                )

        if final_output.blocked_steps:
            parts.append("\n--- INCOMPLETE ANALYSES ---")
            for b in final_output.blocked_steps:
                parts.append(f"  {b.specialist}: {b.error}")

        parts.append(f"\nSpecialists consulted: {', '.join(final_output.specialists_consulted)}")

        return "\n".join(parts)

    def converse(self, user_message: str, context: str = "") -> str:
        prompt = CHAT_SYSTEM_PROMPT
        if context:
            prompt += f"\n\nAnalysis context:\n{context}"

        msg = user_message
        result = self.firewall.call(prompt, msg)

        if result.status == "blocked":
            return "I'm unable to respond to that question due to content restrictions. Could you rephrase?"

        data = result.data or {}
        return data.get("raw", str(data))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_orchestrator/test_chat_agent.py -v`
Expected: All 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/chat_agent.py tests/test_orchestrator/test_chat_agent.py
git commit -m "feat: add chat agent for reviewer interaction"
```

---

## Task 15: CLI Entry Point (main.py)

**Files:**
- Create: `main.py`

- [ ] **Step 1: Implement main.py**

```python
# main.py
"""
Agentic Case Review v7 — POC CLI Entry Point

Usage:
    python main.py --pillar credit_risk --question "What is the delinquency risk?"
    python main.py --pillar credit_risk --mode report
    python main.py --pillar credit_risk  (interactive chat mode)
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid

from dotenv import load_dotenv

load_dotenv()

from agents.general_specialist import GeneralSpecialist
from agents.session_registry import SessionRegistry
from config.pillar_loader import PillarLoader
from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway
from data.generator import DataGenerator
from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger
from orchestrator.chat_agent import ChatAgent
from orchestrator.orchestrator import Orchestrator
from orchestrator.team import TeamConstructor
from skills.domain.loader import load_domain_skill, list_domain_skills
from tools.data_tools import init_tools


def build_adapter(args):
    """Build the appropriate LLM adapter based on args."""
    if args.use_env_pipeline:
        # SafeChain — import only in deployment environment
        from gateway.safechain_adapter import SafeChainAdapter
        # SAFECHAIN SEAM: instantiate safechain LLM object here
        # from safechain.lcel import model as safechain_model
        # llm = safechain_model(args.model)
        raise NotImplementedError(
            "SafeChain adapter requires the deployment environment. "
            "Use --model gpt-4.1 without --use-env-pipeline for local dev."
        )
    else:
        from gateway.openai_adapter import OpenAIAdapter
        return OpenAIAdapter(model=args.model)


def run_question(question: str, mode: str, orchestrator: Orchestrator,
                 team_constructor: TeamConstructor, general_specialist: GeneralSpecialist,
                 chat_agent: ChatAgent, firewall: FirewallStack, logger: EventLogger,
                 pillar: str, registry: SessionRegistry, pillar_loader: PillarLoader):
    """Process a single question through the full pipeline."""
    trace_id = f"q-{uuid.uuid4().hex[:8]}"
    logger.set_trace(trace_id)

    # 1. Team construction
    available = list_domain_skills()
    active = registry.list_active()
    logger.log("orchestrator_dispatch", {
        "question": question, "mode": mode, "pillar": pillar,
    })
    selected = team_constructor.select_specialists(question, pillar, available, active)
    logger.log("orchestrator_dispatch", {
        "question": question, "specialists": selected,
    })

    print(f"\n  Specialists selected: {', '.join(selected)}")

    # 2. Run specialists in sequence (parallel in future)
    specialist_outputs = {}
    for domain in selected:
        print(f"  Running {domain}...", end=" ", flush=True)
        skill = load_domain_skill(domain)
        if skill is None:
            print("SKIP (skill not found)")
            continue
        spec_config = pillar_loader.get_specialist_config(pillar, domain) or {}
        agent = registry.get_or_create(domain, pillar, skill, spec_config, firewall, logger)
        output = agent.run(question, mode)
        specialist_outputs[domain] = output
        print(f"done")

    # 3. General Specialist Compare (if multiple specialists)
    review_report = general_specialist.compare(specialist_outputs, question)
    if review_report.resolved:
        print(f"  Contradictions resolved: {len(review_report.resolved)}")
    if review_report.open_conflicts:
        print(f"  Open conflicts: {len(review_report.open_conflicts)}")

    # 4. Orchestrator Synthesize
    print("  Synthesizing final answer...", end=" ", flush=True)
    final = orchestrator.synthesize(specialist_outputs, review_report, question, mode)
    print("done")

    # 5. Format for reviewer
    formatted = chat_agent.format_for_reviewer(final)
    logger.clear_trace()
    return formatted


def main():
    parser = argparse.ArgumentParser(description="Agentic Case Review v7 — POC")
    parser.add_argument("--pillar", type=str, default="credit_risk",
                        choices=["credit_risk", "escalation", "cbo"])
    parser.add_argument("--question", type=str, default=None,
                        help="Single question to ask (non-interactive mode)")
    parser.add_argument("--mode", type=str, default="chat", choices=["chat", "report"])
    parser.add_argument("--model", type=str, default="gpt-4.1")
    parser.add_argument("--use-env-pipeline", action="store_true",
                        help="Use SafeChain adapter (deployment only)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for data generation")
    args = parser.parse_args()

    # Session setup
    session_id = f"session-{uuid.uuid4().hex[:8]}"
    logger = EventLogger(session_id=session_id)
    print(f"Session: {session_id}")
    print(f"Pillar: {args.pillar}")
    print(f"Log: logs/{session_id}.jsonl")

    # LLM adapter
    adapter = build_adapter(args)
    firewall = FirewallStack(adapter=adapter, logger=logger)

    # Data
    print("Generating simulated data...", end=" ", flush=True)
    generator = DataGenerator(seed=args.seed)
    tables = generator.generate()
    gateway = SimulatedDataGateway(tables=tables)
    catalog = DataCatalog()
    init_tools(gateway, catalog)
    print(f"done ({sum(len(v) for v in tables.values())} rows across {len(tables)} tables)")

    # Components
    pillar_loader = PillarLoader()
    registry = SessionRegistry()
    team_constructor = TeamConstructor(firewall=firewall, logger=logger)
    general_specialist = GeneralSpecialist(firewall=firewall, logger=logger)
    orchestrator = Orchestrator(
        firewall=firewall, logger=logger, registry=registry, pillar=args.pillar,
    )
    chat_agent = ChatAgent(firewall=firewall, logger=logger)

    logger.log("session_start", {"pillar": args.pillar, "model": args.model})

    # Single question mode
    if args.question:
        result = run_question(
            args.question, args.mode, orchestrator, team_constructor,
            general_specialist, chat_agent, firewall, logger,
            args.pillar, registry, pillar_loader,
        )
        print(f"\n{'='*60}")
        print(result)
        print(f"{'='*60}")
        logger.log("session_end", {})
        return

    # Interactive mode
    print("\nInteractive mode — type your questions (Ctrl+C to exit)")
    print(f"Active specialists will be reused across questions.\n")

    try:
        while True:
            question = input("You: ").strip()
            if not question:
                continue
            result = run_question(
                question, args.mode, orchestrator, team_constructor,
                general_specialist, chat_agent, firewall, logger,
                args.pillar, registry, pillar_loader,
            )
            print(f"\n{result}\n")
    except (KeyboardInterrupt, EOFError):
        print("\n\nSession ended.")
        active = registry.list_active()
        if active:
            print(f"Specialists used: {', '.join(a['domain'] for a in active)}")
        logger.log("session_end", {})


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it runs (dry run — will fail without API key, but should parse args)**

Run: `python main.py --help`
Expected: Shows help text with `--pillar`, `--question`, `--mode`, `--model` options

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add CLI entry point with interactive and single-question modes"
```

---

## Task 16: SafeChain Adapter (Stub)

**Files:**
- Create: `gateway/safechain_adapter.py`

This is a stub — fully wired for the deployment environment but raises `NotImplementedError` when SafeChain is not available.

- [ ] **Step 1: Implement stub**

```python
# gateway/safechain_adapter.py
"""
SafeChain LLM Adapter — for deployment environment only.

SAFECHAIN SEAM: This adapter expects a safechain LLM object.
In the deployment environment:
    from safechain.lcel import model as safechain_model
    llm = safechain_model("gpt-4.1")
    adapter = SafeChainAdapter(llm=llm)

Locally, use OpenAIAdapter instead.
"""
from __future__ import annotations

import json
import os
import re

from pydantic import BaseModel
from gateway.llm_adapter import BaseLLMAdapter
from gateway.firewall_stack import FirewallRejection

_ROLE_LABELS = {"system": "Context", "user": "Request", "assistant": "Response"}

TOOL_SCHEMA_HEADER = """You have access to the following tools. To call a tool, respond with JSON:
{"tool_call": {"name": "<tool_name>", "args": {<arguments>}}}

When you have your final answer, respond with JSON:
{"output": {<your structured output>}}

Available tools:
"""


class SafeChainAdapter(BaseLLMAdapter):
    def __init__(self, llm, model_name: str = "gpt-4.1", max_iterations: int = 12):
        self._llm = llm
        self._model_name = model_name
        self._max_iterations = max_iterations

    def run(self, system_prompt: str, user_message: str,
            tools: list | None = None,
            output_type: type[BaseModel] | None = None,
            max_turns: int = 12) -> dict:
        messages = [
            {"role": "system", "content": system_prompt},
        ]

        if tools:
            tool_block = self._build_tool_schema_block(tools)
            messages[0]["content"] += "\n\n" + tool_block

        messages.append({"role": "user", "content": user_message})
        tool_map = {fn.__name__: fn for fn in (tools or [])}

        for _ in range(min(max_turns, self._max_iterations)):
            response = self._invoke(messages)

            # Try to parse as JSON
            try:
                parsed = json.loads(response)
            except json.JSONDecodeError:
                return {"raw": response}

            # Tool call
            if "tool_call" in parsed:
                tc = parsed["tool_call"]
                fn = tool_map.get(tc.get("name", ""))
                if fn:
                    result = fn(**tc.get("args", {}))
                else:
                    result = f"Error: unknown tool '{tc.get('name')}'"
                # Truncate large results
                result_str = str(result)
                if len(result_str) > 3000:
                    result_str = result_str[:3000] + "\n[truncated]"
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": f"Tool result:\n{result_str}"})
                continue

            # Final output
            if "output" in parsed:
                return parsed["output"]

            return parsed

        return {"error": "max iterations exceeded"}

    def chat_turn(self, messages: list[dict]) -> str:
        return self._invoke(messages)

    def _invoke(self, messages: list[dict]) -> str:
        """Call SafeChain LLM with firewall-aware message formatting."""
        try:
            from safechain.prompts import ValidChatPromptTemplate
        except ImportError:
            raise NotImplementedError(
                "SafeChain is not available in this environment. "
                "Use OpenAIAdapter for local development."
            )

        # Combine all messages with neutral role labels
        combined = "\n\n".join(
            f"{_ROLE_LABELS.get(m['role'], m['role'].title())}:\n{m['content']}"
            for m in messages
        )

        # Sanitize: mask long digit sequences, strip code keywords from tool results
        combined = re.sub(r'\b\d{10,}\b', '***MASKED***', combined)

        chain = ValidChatPromptTemplate.from_messages([
            ("human", "{__input__}"),
        ]) | self._llm

        try:
            result = chain.invoke({"__input__": combined})
            return result.content
        except Exception as e:
            error_str = str(e)
            if "401" in error_str:
                # Token expiry — refresh and retry
                self._refresh_llm()
                chain = ValidChatPromptTemplate.from_messages([
                    ("human", "{__input__}"),
                ]) | self._llm
                result = chain.invoke({"__input__": combined})
                return result.content
            if "403" in error_str or "400" in error_str:
                raise FirewallRejection(
                    code=int(re.search(r'(\d{3})', error_str).group(1)) if re.search(r'(\d{3})', error_str) else 403,
                    message=error_str,
                )
            raise

    def _refresh_llm(self) -> None:
        try:
            from safechain.lcel import model as safechain_model
            self._llm = safechain_model(os.environ.get("SAFECHAIN_MODEL", self._model_name))
        except ImportError:
            pass

    def _build_tool_schema_block(self, tools: list) -> str:
        import inspect
        lines = [TOOL_SCHEMA_HEADER]
        for fn in tools:
            sig = inspect.signature(fn)
            params = {n: str(p.annotation.__name__) if p.annotation != inspect.Parameter.empty else "string"
                      for n, p in sig.parameters.items()}
            lines.append(f"- {fn.__name__}({', '.join(f'{k}: {v}' for k, v in params.items())})")
            if fn.__doc__:
                lines.append(f"  {fn.__doc__.strip()}")
        return "\n".join(lines)
```

- [ ] **Step 2: Commit**

```bash
git add gateway/safechain_adapter.py
git commit -m "feat: add SafeChain adapter stub for deployment environment"
```

---

## Task 17: End-to-End Smoke Test

**Files:**
- Create: `tests/test_e2e/__init__.py`
- Create: `tests/test_e2e/test_smoke.py`

This test wires everything together with mocked LLM responses to verify the full pipeline works.

- [ ] **Step 1: Write smoke test**

```python
# tests/test_e2e/test_smoke.py
"""End-to-end smoke test with mocked LLM."""
import pytest
from unittest.mock import MagicMock

from agents.general_specialist import GeneralSpecialist
from agents.session_registry import SessionRegistry
from config.pillar_loader import PillarLoader
from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway
from data.generator import DataGenerator
from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger
from models.types import FinalOutput, LLMResult
from orchestrator.chat_agent import ChatAgent
from orchestrator.orchestrator import Orchestrator
from orchestrator.team import TeamConstructor
from skills.domain.loader import load_domain_skill
from tools.data_tools import init_tools


@pytest.fixture
def full_system(tmp_path):
    """Wire up the full system with mocked LLM."""
    logger = EventLogger(session_id="smoke-test", log_dir=str(tmp_path))
    adapter = MagicMock()
    firewall = FirewallStack(adapter=adapter, logger=logger)

    # Generate data
    gen = DataGenerator(seed=42)
    tables = gen.generate()
    gateway = SimulatedDataGateway(tables=tables)
    catalog = DataCatalog()
    init_tools(gateway, catalog)

    registry = SessionRegistry()
    pillar_loader = PillarLoader()

    return {
        "logger": logger,
        "firewall": firewall,
        "registry": registry,
        "pillar_loader": pillar_loader,
        "gateway": gateway,
        "catalog": catalog,
    }


def test_full_pipeline_smoke(full_system):
    """Test the full pipeline: team construction → specialists → compare → synthesize."""
    fw = full_system["firewall"]
    logger = full_system["logger"]
    registry = full_system["registry"]

    # Mock all LLM calls to return reasonable responses
    call_count = {"n": 0}

    def mock_call(system_prompt, user_message, tools=None, output_type=None):
        call_count["n"] += 1
        # Team construction
        if "select" in system_prompt.lower() and "specialist" in system_prompt.lower():
            return LLMResult(status="success", data={"specialists": ["bureau", "spend_payments"]})
        # Data request
        if "DATA_REQUEST" in user_message:
            return LLMResult(status="success", data={
                "intent": "test", "variables": ["score"], "table_hints": ["bureau_full"],
            })
        # Synthesize (specialist)
        if "SYNTHESIZE" in user_message:
            return LLMResult(status="success", data={
                "findings": "Test findings", "evidence": ["evidence1"],
                "implications": ["implication1"], "data_gaps": [],
            })
        # Answer
        if "ANSWER" in user_message:
            return LLMResult(status="success", data={
                "answer": "Test answer", "evidence": ["evidence1"],
            })
        # Compare
        if "PAIRWISE COMPARE" in user_message:
            return LLMResult(status="success", data={
                "contradictions": [], "cross_domain_insights": ["insight1"],
            })
        # Orchestrator synthesize
        if "orchestrator synthesizer" in system_prompt.lower() or "merge specialist" in system_prompt.lower():
            return LLMResult(status="success", data={
                "answer": "Final synthesized answer based on bureau and payment data.",
                "data_gap_assessments": [],
            })
        return LLMResult(status="success", data={"raw": "default response"})

    fw.call = MagicMock(side_effect=mock_call)

    # Run pipeline
    team_constructor = TeamConstructor(firewall=fw, logger=logger)
    general_specialist = GeneralSpecialist(firewall=fw, logger=logger)
    orchestrator = Orchestrator(
        firewall=fw, logger=logger, registry=registry, pillar="credit_risk",
    )
    chat_agent = ChatAgent(firewall=fw, logger=logger)
    pillar_loader = full_system["pillar_loader"]

    # 1. Team construction
    selected = team_constructor.select_specialists(
        "What is the delinquency risk?", "credit_risk",
        ["bureau", "crossbu", "modeling", "spend_payments"], [],
    )
    assert len(selected) >= 1

    # 2. Run specialists
    specialist_outputs = {}
    for domain in selected:
        skill = load_domain_skill(domain)
        spec_config = pillar_loader.get_specialist_config("credit_risk", domain) or {}
        agent = registry.get_or_create(domain, "credit_risk", skill, spec_config, fw, logger)
        output = agent.run("What is the delinquency risk?", "chat")
        specialist_outputs[domain] = output

    assert len(specialist_outputs) >= 1

    # 3. Compare
    review_report = general_specialist.compare(specialist_outputs, "What is the delinquency risk?")
    assert review_report is not None

    # 4. Synthesize
    final = orchestrator.synthesize(
        specialist_outputs, review_report, "What is the delinquency risk?", "chat",
    )
    assert isinstance(final, FinalOutput)
    assert len(final.answer) > 0

    # 5. Format
    formatted = chat_agent.format_for_reviewer(final)
    assert isinstance(formatted, str)
    assert len(formatted) > 0

    # Verify specialist reuse
    active = registry.list_active()
    assert len(active) >= 1


def test_specialist_reuse_across_questions(full_system):
    """Test that specialists are reused across questions."""
    fw = full_system["firewall"]
    logger = full_system["logger"]
    registry = full_system["registry"]
    pillar_loader = full_system["pillar_loader"]

    fw.call = MagicMock(return_value=LLMResult(
        status="success", data={"findings": "test", "evidence": [], "implications": [], "data_gaps": []},
    ))

    skill = load_domain_skill("bureau")
    spec_config = pillar_loader.get_specialist_config("credit_risk", "bureau") or {}

    # First question
    agent1 = registry.get_or_create("bureau", "credit_risk", skill, spec_config, fw, logger)
    agent1._update_rolling_summary("Q1", "Finding 1")

    # Second question — should get same agent
    agent2 = registry.get_or_create("bureau", "credit_risk", skill, spec_config, fw, logger)
    assert agent1 is agent2
    assert "Q1" in agent2.rolling_summary
```

- [ ] **Step 2: Run smoke test**

Run: `pytest tests/test_e2e/test_smoke.py -v`
Expected: All 2 tests PASS

- [ ] **Step 3: Run full test suite**

Run: `pytest -v`
Expected: All tests PASS across all modules

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e/
git commit -m "test: add end-to-end smoke test for full pipeline"
```

---

## Task 18: Final Verification & Cleanup

- [ ] **Step 1: Run full test suite**

Run: `pytest -v --tb=short`
Expected: All tests PASS

- [ ] **Step 2: Verify CLI runs**

Run: `python main.py --help`
Expected: Help text displayed

- [ ] **Step 3: Verify data generation**

Run: `python -m data.generator --output data/simulated/ --seed 42`
(Note: This requires adding `__main__.py` to data/ — see step 4)

- [ ] **Step 4: Add data generator CLI**

```python
# data/__main__.py
"""Run: python -m data --output data/simulated/ --seed 42"""
import argparse
from data.generator import DataGenerator


def main():
    parser = argparse.ArgumentParser(description="Generate simulated data from YAML profiles")
    parser.add_argument("--output", type=str, default="data/simulated/")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--row-count", type=int, default=None)
    parser.add_argument("--profile-dir", type=str, default="config/data_profiles")
    args = parser.parse_args()

    gen = DataGenerator(profile_dir=args.profile_dir, seed=args.seed)
    tables = gen.generate(row_count_override=args.row_count)
    gen.dump_csv(tables, args.output)

    total_rows = sum(len(v) for v in tables.values())
    print(f"Generated {total_rows} rows across {len(tables)} tables → {args.output}")


if __name__ == "__main__":
    main()
```

Run: `python -m data --output data/simulated/ --seed 42`
Expected: CSV files generated in `data/simulated/`

- [ ] **Step 5: Final commit**

```bash
git add data/__main__.py
git add -A  # catch any missed __init__.py files
git commit -m "feat: v7 POC framework complete — all components wired"
```
