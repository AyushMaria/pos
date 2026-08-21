"""The sync engine — architecture §9.

Push is append-only and idempotent; pull is a per-entity watermark. No row is
written by both sides, so there is nothing to merge.
"""
