"""Where a job board becomes the postings this app stores: one module per source.

There is no base class, protocol or registry here yet, and deliberately so: the
Greenhouse adapter is the only source that exists, and an interface with one
implementation is a guess. #28 brings the second adapter and whatever the two
have in common.
"""
