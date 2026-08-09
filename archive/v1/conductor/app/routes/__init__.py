"""The HTTP surface, split by domain — one package where routes.py was one file.

Importing this package is what registers every endpoint: each domain module
adds its routes to the shared router in base.py at import time, so the import
list below IS the route-registration order. main.py still mounts everything
with one include, and everything that was importable from routes.py — the
guards, `_manager_tasks`, the Settings and Worker* models, and the app modules
that tests monkeypatch through (`app.routes.manager.run_manager` and friends)
— is still importable from app.routes, so no caller had to learn the split.
"""

from .base import (_manager_tasks, _owned, _root, can_see,      # noqa: F401
                   current_user, owned_project, owned_table, owned_task, router)

# routes.py imported these app modules, which made "app.routes.credcheck.check",
# "app.routes.manager.run_manager" and "app.routes.notify.report_error" the
# paths tests monkeypatch. Keep those exact attribute paths alive.
from .. import credcheck, manager, notify                       # noqa: F401

# Domain modules, in the order their endpoints appeared in the one-file days.
from . import session                                           # noqa: F401,E402
from . import agent_ops                                         # noqa: F401,E402
from . import deploy                                            # noqa: F401,E402
from . import planning                                          # noqa: F401,E402
from . import platform_self                                     # noqa: F401,E402
from . import studio_legacy                                     # noqa: F401,E402
from . import misc                                              # noqa: F401,E402
from . import selfhost                                          # noqa: F401,E402
from . import projects                                          # noqa: F401,E402
from . import boss                                              # noqa: F401,E402
from . import tasks                                             # noqa: F401,E402
from . import internal                                          # noqa: F401,E402
from . import graph                                             # noqa: F401,E402  the module graph
from . import graph_project                                     # noqa: F401,E402  its project tenant
from . import svc                                               # noqa: F401,E402  the /svc fleet gateway

# Names other code imports from app.routes directly.
from .session import Settings                                   # noqa: F401,E402
from .internal import WorkerEvent, WorkerReport                 # noqa: F401,E402
