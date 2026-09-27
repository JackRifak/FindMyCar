"""Deliverable 4: pluggable, benchmarkable VPR embedding models.

Each embedder here implements BenchmarkEmbedder (fmc.vpr.embedders.base) and
is registered in fmc.vpr.embedders.registry under a short name, so
scripts/benchmark_embedders.py can compare them side by side on a site's
real ingested data. This package is separate from the production
fmc.vpr.embedder.get_embedder() factory used by fmc.vpr.pipeline -- swap
that factory's return value only once benchmarking here has picked a
winner.
"""
