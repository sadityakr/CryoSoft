"""Troubleshoot toolbox — diagnostic primitives for commissioning and debugging.

This package sits beside the layer stack (like ``cryosoft.main``): it may
import drivers, the Station's config helpers, and the foundation, but nothing
in cryosoft imports it. It is used by the troubleshoot CLI and by agents
diagnosing a setup; the main application never touches it.
"""
