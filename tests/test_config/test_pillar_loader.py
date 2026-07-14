"""Tests for config.pillar_loader.PillarLoader."""

from __future__ import annotations

import os

import pytest

from config.pillar_loader import PillarLoader

PILLAR_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "config", "pillars")


@pytest.fixture
def loader():
    return PillarLoader(pillar_dir=PILLAR_DIR)


class TestPillarLoader:
    def test_load_credit_risk(self, loader: PillarLoader):
        pillar = loader.load("credit_risk")
        assert pillar is not None
        assert pillar["pillar"] == "credit_risk"
        assert pillar["display_name"] == "Credit & Risk (Commercial / SBS)"
        assert "specialists" in pillar

    def test_specialist_has_focus_and_overlay(self, loader: PillarLoader):
        pillar = loader.load("credit_risk")
        bureau = pillar["specialists"]["bureau"]
        assert "focus" in bureau
        assert "prompt_overlay" in bureau
        assert bureau["focus"] == "Delinquency Risk"
        assert bureau["prompt_overlay"] == "Flag 90D+ Marks"

    def test_load_all_pillars(self, loader: PillarLoader):
        names = loader.list_pillars()
        assert set(names) == {"cbo", "credit_risk", "escalation"}

    def test_nonexistent_returns_none(self, loader: PillarLoader):
        result = loader.load("nonexistent_pillar")
        assert result is None

    def test_get_specialist_config(self, loader: PillarLoader):
        spec = loader.get_specialist_config("credit_risk", "wcc")
        assert spec is not None
        assert spec["focus"] == "Watch-list Flags"
        assert spec["prompt_overlay"] == "Standalone signals"

    def test_get_specialist_config_nonexistent_pillar(self, loader: PillarLoader):
        result = loader.get_specialist_config("nonexistent", "bureau")
        assert result is None

    def test_get_specialist_config_nonexistent_domain(self, loader: PillarLoader):
        result = loader.get_specialist_config("credit_risk", "nonexistent_domain")
        assert result is None

    def test_caching(self, loader: PillarLoader):
        p1 = loader.load("credit_risk")
        p2 = loader.load("credit_risk")
        assert p1 is p2  # same object reference = cached
