"""Shared app state (per browser session)."""
import solara

# Bumped after a bake/extend/delete so the Explore tab re-reads the registry.
registry_version = solara.reactive(0)

# Admin session flag: set by the shared admin password, or by signing in with an
# admin account (see main.Page).
is_admin = solara.reactive(False)

# The signed-in account (accounts.User), or None. Set from the session cookie.
user = solara.reactive(None)

# Bumped after a verdict is saved or vetoed so views re-read the validations.
validation_version = solara.reactive(0)
