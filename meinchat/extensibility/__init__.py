"""meinchat extension seams (S28.3a).

Narrow ports + an in-process registry that downstream plugins (meinchat-plus)
register impls against in `on_enable` and unregister in `on_disable`. meinchat
resolves them at call time. With no downstream plugin loaded the registered
defaults preserve today's behaviour byte-for-byte (the "plugin-free still works"
oracle is the regression net).
"""
