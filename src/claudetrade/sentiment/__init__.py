"""Sentiment pipeline: entity resolution, classification, and aggregation.

Pipeline order: ``TickerResolver`` finds which symbols a post is about ->
``RuleSentimentClassifier`` / ``EnsembleSentimentClassifier`` score each
(post, symbol) pair -> ``ManipulationDetector`` flags coordinated/bot activity
across a post set -> ``SentimentAggregator`` folds everything into one
point-in-time ``SymbolSentiment`` per session, with no look-ahead.

Every stage is usable with zero AI credentials configured; see
``sentiment.ai_classifier`` for the optional AI-assisted path and its
degrade-to-rules-on-any-failure contract.
"""

from __future__ import annotations

from claudetrade.sentiment.aggregation import SentimentAggregator, time_decay_weight
from claudetrade.sentiment.ai_classifier import AISentimentClassifier, InMemoryAICache
from claudetrade.sentiment.classifiers import EnsembleSentimentClassifier, RuleSentimentClassifier
from claudetrade.sentiment.entity_resolution import EXTRACTION_VERSION, TickerResolver
from claudetrade.sentiment.manipulation import ManipulationAssessment, ManipulationDetector

__all__ = [
    "EXTRACTION_VERSION",
    "AISentimentClassifier",
    "EnsembleSentimentClassifier",
    "InMemoryAICache",
    "ManipulationAssessment",
    "ManipulationDetector",
    "RuleSentimentClassifier",
    "SentimentAggregator",
    "TickerResolver",
    "time_decay_weight",
]
