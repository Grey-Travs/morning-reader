"""Morning Reader's engine: Japanese novels and manga, translated and read locally.

Nothing in this package imports the web layer, so the engine can be driven from a
script, a test, or the FastAPI server in ``server/`` without dragging FastAPI in.

Naming rule, load-bearing: the language of the original is always ``source``, never
``japanese`` and never the name of any one script. Night Reader persisted the string
``"korean"`` in three on-disk contracts and that is the single reason this app is a
separate app rather than a branch of it. Keys such as ``source_hash`` and
``source_fraction`` are written to disk; renaming one later means migrating every
project on the shelf.
"""
