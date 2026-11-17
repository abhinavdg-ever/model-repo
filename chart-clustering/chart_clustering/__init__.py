"""Chart clustering: unsupervised chart segments from each chart's anchor pages, by their
text (template words + layout) and their look (page-image fingerprint), fused by default.

See ../METHODOLOGY.md (how it works) and ../README.md (how to run it). No page text, names, DOBs
or member ids ever leave this package's outputs: features are numbers, and cluster cards show
only whitelisted document words.
"""
__version__ = "0.2.0"
