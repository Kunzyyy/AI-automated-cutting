"""Offline-first footage analysis for the V2 video workflow."""

from .pipeline import AnalyzerConfig, analyze_directory

__all__ = ["AnalyzerConfig", "analyze_directory"]
