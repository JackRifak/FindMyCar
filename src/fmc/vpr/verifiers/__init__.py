"""Deliverable 5: pluggable, benchmarkable second-stage geometric verifiers.

Each verifier here implements BenchmarkVerifier (fmc.vpr.verifiers.base) and
is registered in fmc.vpr.verifiers.registry under a short name, so
scripts/benchmark_verifiers.py can compare them on the site's own real
true-match / hard-negative pairs. Separate from the production
fmc.vpr.geometric_verification.verify() used by fmc.vpr.pipeline -- swap
that module's implementation only once benchmarking here has picked a
winner.
"""
