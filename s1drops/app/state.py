"""Shared app state."""
import solara

# Bumped after a bake/extend/delete so the Explore tab re-reads the registry.
registry_version = solara.reactive(0)

# Admin session flag (set only by a successful server-side password check).
is_admin = solara.reactive(False)
