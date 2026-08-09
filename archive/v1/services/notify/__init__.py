# Package marker for TEST COLLECTION ONLY.
#
# pytest's prepend import mode names a test module after the first parent
# directory WITHOUT an __init__.py. Without this file (and the one in tests/),
# every service's tests/conftest.py would register as the same top-level module
# `conftest` — the second one imported silently replaces the first, and the
# conductor suite's `from conftest import login` then reaches into a service's
# test harness. With it, this service's conftest is `notify.tests.conftest`:
# unique per service, because the package name IS the service directory name.
#
# It changes nothing at runtime: the service is launched as `uvicorn app:app
# --app-dir services/notify`, which puts the directory on sys.path rather than
# importing it as a package, and nothing outside the directory imports in.
